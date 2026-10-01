import frappe
from frappe import _

from commera.qikink.items import get_qikink_lines, is_qikink_item, is_template_drop_ship


def validate_qikink_item(item, method=None) -> None:
	"""A Qikink item holds no stock, and each sellable one names its Qikink SKU."""
	if not is_qikink_item(item):
		return
	if item.is_stock_item:
		frappe.throw(_("{0} is a Qikink item, so it cannot be a stock item.").format(item.item_code))
	if not item.has_variants and not item.custom_qikink_sku:
		frappe.throw(_("Set the Qikink SKU of {0}. Qikink prints and ships it.").format(item.item_code))


def set_template_drop_ship_onload(item, method=None) -> None:
	"""Show the Qikink SKU on a variant whose drop-ship tick is on its template only."""
	item.set_onload("template_is_drop_ship", is_template_drop_ship(item))


def create_qikink_order(sales_order, method=None) -> None:
	"""Queue the push of the Qikink lines of a submitted Sales Order."""
	if not frappe.get_cached_doc("Qikink Settings").enabled or not get_qikink_lines(sales_order):
		return
	qikink_order = frappe.get_doc({"doctype": "Qikink Order", "sales_order": sales_order.name})
	qikink_order.insert(ignore_permissions=True)
	qikink_order.queue_push()


def set_qikink_order_onload(doc, method=None) -> None:
	"""Hand the Sales Order or Purchase Order form its Qikink Order, for the Qikink indicator."""
	if not frappe.has_permission("Qikink Order", "read"):
		return
	link_field = "sales_order" if doc.doctype == "Sales Order" else "purchase_order"
	qikink_order = frappe.db.get_value(
		"Qikink Order", {link_field: doc.name}, ["name", "order_number", "status"], as_dict=True
	)
	if qikink_order:
		doc.set_onload("qikink_order", qikink_order)
