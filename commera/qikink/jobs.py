import frappe


def push_qikink_order(qikink_order: str) -> None:
	frappe.get_doc("Qikink Order", qikink_order).push()
