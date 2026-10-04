from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictInt, StrictStr

from .config import Settings, load_settings
from .errors import AppError
from .payments import PaymentGateway
from .services import Shop


class AddItemBody(BaseModel):
    product_id: StrictStr
    quantity: StrictInt


class QuantityBody(BaseModel):
    quantity: StrictInt


class CheckoutBody(BaseModel):
    coupon_code: StrictStr | None = None
    expected_subtotal_cents: StrictInt | None = None


class ProductPatchBody(BaseModel):
    price_cents: StrictInt | None = None
    inventory: StrictInt | None = None


def create_app(settings: Settings | None = None, payments: PaymentGateway | None = None) -> FastAPI:
    shop = Shop(settings or load_settings(), payments)
    app = FastAPI(title="Checkout & Rewards Service")
    app.state.shop = shop

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"], "type": e["type"]}
                   for e in exc.errors()]
        err = AppError(422, "VALIDATION_ERROR", "Request body is malformed or has wrong types", details)
        return JSONResponse(status_code=422, content=err.body())

    # ---- storefront -------------------------------------------------------
    @app.get("/products")
    def list_products():
        return shop.list_products()

    @app.post("/carts", status_code=201)
    def create_cart():
        return shop.create_cart()

    @app.get("/carts/{cart_id}")
    def get_cart(cart_id: str):
        return shop.get_cart(cart_id)

    @app.post("/carts/{cart_id}/items", status_code=201)
    def add_item(cart_id: str, body: AddItemBody):
        return shop.add_item(cart_id, body.product_id, body.quantity)

    @app.patch("/carts/{cart_id}/items/{product_id}")
    def set_quantity(cart_id: str, product_id: str, body: QuantityBody):
        return shop.set_item_quantity(cart_id, product_id, body.quantity)

    @app.delete("/carts/{cart_id}/items/{product_id}")
    def remove_item(cart_id: str, product_id: str):
        return shop.remove_item(cart_id, product_id)

    @app.post("/carts/{cart_id}/checkout")
    def checkout(cart_id: str, response: Response, body: CheckoutBody | None = None):
        body = body or CheckoutBody()
        order, replayed = shop.checkout(cart_id, body.coupon_code, body.expected_subtotal_cents)
        response.status_code = 200 if replayed else 201
        if replayed:
            response.headers["Idempotent-Replay"] = "true"
        return order

    @app.get("/orders/{order_id}")
    def get_order(order_id: str):
        return shop.get_order(order_id)

    # ---- administrative (no authn/authz implemented; see DECISIONS.md) ----
    @app.post("/admin/coupons/generate", status_code=201)
    def generate_coupon():
        return shop.generate_coupon()

    @app.get("/admin/coupons")
    def list_coupons():
        return shop.list_coupons()

    @app.get("/admin/orders")
    def list_orders():
        return shop.list_orders()

    @app.get("/admin/report")
    def report():
        return shop.report()

    @app.patch("/admin/products/{product_id}")
    def patch_product(product_id: str, body: ProductPatchBody):
        return shop.update_product(product_id, body.price_cents, body.inventory)

    return app


def app_factory() -> FastAPI:  # for: uvicorn app.main:app_factory --factory
    return create_app()
