import frappe


def execute():
	"""Take the Qikink SKU off the fields ERPNext copies from a template to its variants.
	The setup wizard listed it, so each template save put the template's empty SKU on every variant.
	"""
	frappe.db.delete("Variant Field", {"parent": "Item Variant Settings", "field_name": "custom_qikink_sku"})
