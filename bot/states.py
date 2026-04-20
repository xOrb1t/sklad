from aiogram.fsm.state import State, StatesGroup


class AddProduct(StatesGroup):
    waiting_photo = State()
    waiting_name = State()
    waiting_description = State()
    waiting_sku = State()
    waiting_quantity = State()


class UpdatingQuantity(StatesGroup):
    waiting_qty = State()
