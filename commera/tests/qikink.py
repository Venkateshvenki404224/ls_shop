# Copyright (c) 2026, company@bwhstudios.com and Contributors

import json
from urllib.parse import urlsplit

import frappe
import requests

SETTINGS = "Qikink Settings"
CLIENT_SECRET = "zz-qikink-client-secret"
QIKINK_ORDER_ID = 1234567890
TOKEN_PATH = "/api/token"
CREATE_ORDER_PATH = "/api/order/create"
ORDER_LIST_PATH = "/api/order/list"


def configure_qikink(testcase, supplier: str | None, **values) -> None:
	"""Turn Qikink on in sandbox mode for one test. Its own ClientId keeps cached tokens apart."""
	testcase.addCleanup(frappe.clear_document_cache, SETTINGS, SETTINGS)
	settings = frappe.get_doc(SETTINGS)
	settings.update(
		{
			"enabled": 1,
			"sandbox": 1,
			"client_id": f"zz-client-{frappe.generate_hash(length=8)}",
			"client_secret": CLIENT_SECRET,
			"supplier": supplier,
			"box_packing": 0,
			"brand_logo": None,
			**values,
		}
	)
	settings.save(ignore_permissions=True)


class FakeQikink:
	"""Stands in for the request session of the Qikink client. It records each request and replies as Qikink.

	Each entry of `create_replies` answers one create call: a reply, or an exception to raise. Then 200.
	"""

	def __init__(self):
		self.requests: list[frappe._dict] = []
		self.token_expires_in = 3600
		self.create_replies: list[requests.Response | Exception] = []
		# The orders Qikink holds, as the order list shows them.
		self.orders: list[dict] = []

	def request(self, method: str, url: str, **kwargs) -> requests.Response:
		self.requests.append(frappe._dict(method=method, url=url, **kwargs))
		path = urlsplit(url).path
		if path == TOKEN_PATH:
			return make_reply(
				200,
				{
					"ClientId": kwargs["data"]["ClientId"],
					"Accesstoken": f"zz-access-token-{len(self.get_requests(TOKEN_PATH))}",
					"expires_in": self.token_expires_in,
				},
			)
		if path == CREATE_ORDER_PATH:
			return self.get_create_reply()
		if path == ORDER_LIST_PATH:
			return self.get_order_list(kwargs["params"])
		raise AssertionError(f"Qikink has no {method} {url}")

	def get_requests(self, path: str) -> list[frappe._dict]:
		return [request for request in self.requests if urlsplit(request.url).path == path]

	def get_create_reply(self) -> requests.Response:
		if not self.create_replies:
			return make_reply(
				200,
				{"message": "Order created successfully", "order_id": QIKINK_ORDER_ID, "status_code": "200"},
			)
		reply = self.create_replies.pop(0)
		if isinstance(reply, Exception):
			raise reply
		return reply

	def add_order(self, order_number: str, order_id: int) -> None:
		# Qikink puts the account number in front of the order number.
		self.orders.append({"order_id": order_id, "number": f"1_{order_number}", "status": "Live"})

	def get_order_list(self, params: dict) -> requests.Response:
		reference = params["order_reference_no"]
		orders = [order for order in self.orders if order["number"].endswith(f"_{reference}")]
		return make_reply(200, {"success": True, "data": orders[:10]})


def make_reply(status_code: int, body: dict | str) -> requests.Response:
	"""A reply with a JSON body, or with the text as it stands."""
	reply = requests.Response()
	reply.status_code = status_code
	reply._content = (body if isinstance(body, str) else json.dumps(body)).encode()
	return reply
