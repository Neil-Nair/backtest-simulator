# Response objects returned by insert_order / amend_order / delete_order.


class InsertOrderResponse:
    def __init__(self, success, order_id, error_reason):
        self.success = success
        self.order_id = order_id
        self.error_reason = error_reason

    def __repr__(self):
        return "InsertOrderResponse(success=%s, order_id=%s, error_reason=%r)" % (
            self.success, self.order_id, self.error_reason)


class AmendOrderResponse:
    def __init__(self, success, error_reason):
        self.success = success
        self.error_reason = error_reason

    def __repr__(self):
        return "AmendOrderResponse(success=%s, error_reason=%r)" % (self.success, self.error_reason)


class DeleteOrderResponse:
    def __init__(self, success, error_reason):
        self.success = success
        self.error_reason = error_reason

    def __repr__(self):
        return "DeleteOrderResponse(success=%s, error_reason=%r)" % (self.success, self.error_reason)
