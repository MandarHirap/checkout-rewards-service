import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(settings, gateway):
    return TestClient(create_app(settings, gateway))


def new_cart(client, *items):
    cid = client.post("/carts").json()["id"]
    for pid, q in items:
        assert client.post(f"/carts/{cid}/items", json={"product_id": pid, "quantity": q}).status_code == 201
    return cid


def test_status_codes_and_error_envelope(client):
    cid = new_cart(client)
    r = client.post(f"/carts/{cid}/items", json={"product_id": "nope", "quantity": 1})
    assert r.status_code == 404 and r.json()["error"]["code"] == "PRODUCT_NOT_FOUND"
    r = client.post(f"/carts/{cid}/items", json={"product_id": "sku-tee", "quantity": "2"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = client.post(f"/carts/{cid}/items", json={"product_id": "sku-tee", "quantity": 0})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_QUANTITY"
    assert client.get("/carts/missing").status_code == 404
    assert client.post(f"/carts/{cid}/checkout").json()["error"]["code"] == "EMPTY_CART"


def test_checkout_201_then_200_replay_and_admin_flow(client):
    for _ in range(3):
        cid = new_cart(client, ("sku-notebook", 1))
        assert client.post(f"/carts/{cid}/checkout").status_code == 201
    gen = client.post("/admin/coupons/generate")
    assert gen.status_code == 201
    assert client.post("/admin/coupons/generate").status_code == 409
    cid = new_cart(client, ("sku-tee", 1))
    body = {"coupon_code": gen.json()["code"]}
    first = client.post(f"/carts/{cid}/checkout", json=body)
    replay = client.post(f"/carts/{cid}/checkout", json=body)
    assert (first.status_code, replay.status_code) == (201, 200)
    assert replay.headers["Idempotent-Replay"] == "true" and replay.json() == first.json()
    assert client.get(f"/orders/{first.json()['id']}").json()["total_cents"] == 1799
    assert client.get("/orders/unknown").status_code == 404
    assert client.get("/admin/report").json() == client.get("/admin/report").json()
