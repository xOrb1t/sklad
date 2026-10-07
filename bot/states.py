from aiogram.fsm.state import State, StatesGroup


class AddProduct(StatesGroup):
    waiting_photo = State()
    waiting_name = State()
    waiting_description = State()
    waiting_sku = State()
    waiting_avito = State()
    waiting_quantity = State()


class EditProduct(StatesGroup):
    """Editing one field of an existing product; data: product_id, field."""

    waiting_value = State()
