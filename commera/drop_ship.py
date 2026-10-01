import frappe
from frappe.query_builder import DocType
from frappe.utils import add_days, cint, create_batch

from commera.utils import IN_CLAUSE_CHUNK_SIZE

# The supplier holds the stock, so the storefront sells up to this many per line.
DROP_SHIP_STOCK_QTY = 100


def get_drop_ship_item_codes(item_codes: list[str]) -> set[str]:
	"""The item codes ticked "Delivered by Supplier (Drop Ship)" on the item or on its template."""
	item = DocType("Item")
	template = DocType("Item").as_("template")
	drop_ship_item_codes = set()
	for item_code_chunk in create_batch(item_codes, IN_CLAUSE_CHUNK_SIZE):
		drop_ship_item_codes.update(
			(
				frappe.qb.from_(item)
				.left_join(template)
				.on(template.name == item.variant_of)
				.select(item.name)
				.where(item.name.isin(item_code_chunk))
				.where((item.delivered_by_supplier == 1) | (template.delivered_by_supplier == 1))
			).run(pluck=True)
		)
	return drop_ship_item_codes


def get_supplier_details(item_codes: list[str], company: str) -> dict[str, frappe._dict]:
	"""The company's default supplier and the supplier lead time of each item, from the item or else its template."""
	item = DocType("Item")
	template = DocType("Item").as_("template")
	own_default = DocType("Item Default").as_("own_default")
	template_default = DocType("Item Default").as_("template_default")
	rows = (
		frappe.qb.from_(item)
		.left_join(template)
		.on(template.name == item.variant_of)
		.left_join(own_default)
		.on(
			(own_default.parenttype == "Item")
			& (own_default.parent == item.name)
			& (own_default.company == company)
		)
		.left_join(template_default)
		.on(
			(template_default.parenttype == "Item")
			& (template_default.parent == item.variant_of)
			& (template_default.company == company)
		)
		.select(
			item.name,
			own_default.default_supplier.as_("own_supplier"),
			template_default.default_supplier.as_("template_supplier"),
			item.lead_time_days.as_("own_lead_time_days"),
			template.lead_time_days.as_("template_lead_time_days"),
		)
		.where(item.name.isin(item_codes))
	).run(as_dict=True)
	return {
		row.name: frappe._dict(
			supplier=row.own_supplier or row.template_supplier,
			lead_time_days=cint(row.own_lead_time_days or row.template_lead_time_days),
		)
		for row in rows
	}


def set_drop_ship_lines(sales_order) -> None:
	"""Have the supplier deliver each drop-ship line, as "Make Purchase Order" in Desk expects."""
	# The Quotation line has no drop-ship tick, so the mapped order line starts at 0 and ERPNext
	# fills only empty item details: it never copies the tick, and reads no template supplier.
	drop_ship_item_codes = get_drop_ship_item_codes([row.item_code for row in sales_order.items])
	if not drop_ship_item_codes:
		return

	supplier_details = get_supplier_details(list(drop_ship_item_codes), sales_order.company)
	for row in sales_order.items:
		if row.item_code not in drop_ship_item_codes:
			continue
		row.delivered_by_supplier = 1
		row.supplier = supplier_details[row.item_code].supplier or row.supplier
		# A Shopping Cart order needs no delivery date, but the Purchase Order takes its Required By from it.
		row.delivery_date = add_days(
			sales_order.transaction_date, supplier_details[row.item_code].lead_time_days
		)
