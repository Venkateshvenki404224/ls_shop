import re

import frappe
from frappe.utils import cint, cstr, flt, get_url

from commera.api.payments import is_cod

ADDRESS1_LENGTH = 90


def get_order_body(qikink_order) -> dict:
	"""The Qikink create-order body: the Purchase Order lines at the prices the customer paid."""
	settings = frappe.get_cached_doc("Qikink Settings")
	purchase_order = frappe.get_doc("Purchase Order", qikink_order.purchase_order)
	sales_order = frappe.get_doc("Sales Order", qikink_order.sales_order)
	sales_lines = {row.name: row for row in sales_order.items}
	ordered_lines = [(row, sales_lines[row.sales_order_item]) for row in purchase_order.items]
	return {
		"order_number": qikink_order.order_number,
		"qikink_shipping": 1,
		"gateway": "COD" if is_cod(sales_order.custom_ecommerce_payment_mode) else "Prepaid",
		"total_order_value": get_total_order_value(sales_order, [line for _, line in ordered_lines]),
		"line_items": get_line_items(ordered_lines),
		"shipping_address": get_shipping_address(purchase_order),
		"add_ons": [{"box_packing": cint(settings.box_packing)}],
		"brand_logo": get_url(settings.brand_logo) if settings.brand_logo else 0,
	}


def get_total_order_value(sales_order, qikink_lines: list) -> float:
	"""The Qikink share of the grand total, as Qikink collects a COD order's parcel on its own."""
	qikink_share = sum(flt(row.net_amount) for row in qikink_lines) / flt(sales_order.net_total)
	return flt(flt(sales_order.grand_total) * qikink_share, sales_order.precision("grand_total"))


def get_line_items(ordered_lines: list) -> list[dict]:
	skus = dict(
		frappe.get_all(
			"Item",
			filters={"name": ["in", [row.item_code for row, _ in ordered_lines]]},
			fields=["name", "custom_qikink_sku"],
			as_list=True,
		)
	)
	return [
		{
			"search_from_my_products": 1,
			"sku": skus[row.item_code],
			"quantity": cint(row.qty),
			"price": flt(sales_line.net_rate),
		}
		for row, sales_line in ordered_lines
	]


def get_shipping_address(purchase_order) -> dict:
	address = frappe.get_doc("Address", purchase_order.shipping_address)
	address_line = cstr(address.address_line1)
	overflow = (address_line[ADDRESS1_LENGTH:].strip(), cstr(address.address_line2).strip())
	country_code = cstr(frappe.db.get_value("Country", address.country, "code")).upper()
	first_name, last_name = get_consignee_name(purchase_order)
	return {
		"first_name": first_name,
		"last_name": last_name,
		"address1": address_line[:ADDRESS1_LENGTH],
		"address2": ", ".join(part for part in overflow if part),
		"phone": get_phone_number(address.phone or purchase_order.customer_contact_mobile, country_code),
		"email": address.email_id or purchase_order.customer_contact_email,
		"city": address.city,
		"zip": get_digits(address.pincode),
		"province": address.state,
		"country_code": country_code,
	}


def get_consignee_name(purchase_order) -> tuple[str, str]:
	"""The name of the customer contact, or else the customer name."""
	contact = frappe.db.get_value(
		"Contact", purchase_order.customer_contact_person, ["first_name", "last_name"], as_dict=True
	)
	if contact and contact.first_name:
		return contact.first_name, cstr(contact.last_name)
	first_name, _, last_name = cstr(purchase_order.customer_name).partition(" ")
	return first_name, last_name


def get_phone_number(phone: str | None, country_code: str) -> str:
	digits = get_digits(phone)
	# Qikink takes an Indian number as its 10 digits, without the 91 country code.
	if country_code == "IN" and len(digits) == 12 and digits.startswith("91"):
		return digits[2:]
	return digits


def get_digits(value: str | None) -> str:
	return re.sub(r"\D", "", cstr(value))
