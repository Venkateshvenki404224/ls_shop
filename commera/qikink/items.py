import frappe

from commera.drop_ship import get_drop_ship_item_codes, get_supplier_details


def get_qikink_supplier() -> str | None:
	return frappe.get_cached_doc("Qikink Settings").supplier


def is_qikink_item(item) -> bool:
	"""Whether the Item, saved or not, is drop ship with the Qikink Supplier as its default supplier."""
	qikink_supplier = get_qikink_supplier()
	return bool(qikink_supplier) and is_drop_ship(item) and get_default_supplier(item) == qikink_supplier


def get_qikink_lines(sales_order) -> list:
	"""The order lines Qikink delivers: the drop-ship lines of the Qikink Supplier."""
	qikink_supplier = get_qikink_supplier()
	return [
		row
		for row in sales_order.items
		if qikink_supplier and row.delivered_by_supplier and row.supplier == qikink_supplier
	]


def is_drop_ship(item) -> bool:
	return bool(item.delivered_by_supplier) or is_template_drop_ship(item)


def is_template_drop_ship(item) -> bool:
	return bool(item.variant_of and get_drop_ship_item_codes([item.variant_of]))


def get_default_supplier(item) -> str | None:
	"""The Commera company's default supplier on the Item being saved, or else on its template."""
	company = frappe.get_cached_value("Commera Settings", "Commera Settings", "company")
	own_supplier = next((row.default_supplier for row in item.item_defaults if row.company == company), None)
	if own_supplier or not item.variant_of:
		return own_supplier
	return get_supplier_details([item.variant_of], company)[item.variant_of].supplier
