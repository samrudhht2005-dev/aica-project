# Billing cart manager for POS system operations
class POSCartManager:
    def __init__(self):
        self.items = []
        
    def add_item(self, product_name: str, price: float, quantity: float = 1.0):
        for item in self.items:
            if item["product"].lower() == product_name.lower():
                item["quantity"] += quantity
                return item
        new_item = {
            "product": product_name,
            "price": price,
            "quantity": quantity
        }
        self.items.append(new_item)
        return new_item

    def clear(self):
        self.items = []
