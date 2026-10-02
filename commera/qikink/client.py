from http import HTTPStatus

import frappe
import requests
from frappe import _
from frappe.utils import cint, get_request_session

BASE_URLS = {"sandbox": "https://sandbox.qikink.com", "live": "https://api.qikink.com"}
TIMEOUT_SECONDS = 30
TOKEN_EXPIRY_MARGIN_SECONDS = 60


class QikinkUnavailableError(Exception):
	"""Qikink did not answer, asked to wait, or its answer was lost. The same call can succeed later."""


class QikinkRefusedError(frappe.ValidationError):
	"""Qikink refused the call. Repeating it gets the same answer until staff fix the order."""

	def __init__(self, message: str, status_code: int):
		super().__init__(message)
		self.status_code = status_code


class QikinkClient:
	"""The Qikink API of the account in Qikink Settings."""

	def __init__(self):
		self.settings = frappe.get_cached_doc("Qikink Settings")

	@property
	def token_cache_key(self) -> str:
		return f"commera:qikink_access_token:{self.settings.mode}:{self.settings.client_id}"

	def get_access_token(self) -> str:
		"""Reuse the token until a minute before it expires: each new token ends the one before it."""
		if token := frappe.cache.get_value(self.token_cache_key, expires=True):
			return token

		reply = self.send(
			"POST",
			"/api/token",
			data={
				"ClientId": self.settings.client_id,
				"client_secret": self.settings.get_password("client_secret"),
			},
		)
		lifetime = cint(reply.get("expires_in")) - TOKEN_EXPIRY_MARGIN_SECONDS
		if lifetime > 0:
			frappe.cache.set_value(self.token_cache_key, reply["Accesstoken"], expires_in_sec=lifetime)
		return reply["Accesstoken"]

	@property
	def has_order_list(self) -> bool:
		# Qikink notes that the order list does not work in the sandbox now.
		return not self.settings.sandbox

	def get_auth_headers(self) -> dict:
		return {"ClientId": self.settings.client_id, "Accesstoken": self.get_access_token()}

	def create_order(self, body: dict) -> dict:
		reply = self.send_with_token("POST", "/api/order/create", json=body)
		if not reply.get("order_id"):
			# Qikink may hold the order all the same, so a retry must look it up first.
			raise QikinkUnavailableError(_("Qikink sent no order id for the order."))
		return reply

	def get_order(self, order_number: str) -> dict | None:
		"""The Qikink order made under the order number, if Qikink has one."""
		reply = self.send_with_token("GET", "/api/order/list", params={"order_reference_no": order_number})
		# Qikink puts the account number in front of the order number, such as `1_qk00042`.
		return next(
			(order for order in reply["data"] if order["number"].rpartition("_")[2] == order_number), None
		)

	def send_with_token(self, method: str, endpoint: str, **kwargs) -> dict:
		"""A 401 gets one new token: Qikink ends a token when it issues the next one."""
		headers = self.get_auth_headers()
		try:
			return self.send(method, endpoint, headers=headers, **kwargs)
		except QikinkRefusedError as error:
			if error.status_code != HTTPStatus.UNAUTHORIZED:
				raise
		frappe.cache.delete_value(self.token_cache_key)
		return self.send(method, endpoint, headers=self.get_auth_headers(), **kwargs)

	def send(self, method: str, endpoint: str, **kwargs) -> dict:
		try:
			response = get_request_session().request(
				method, f"{BASE_URLS[self.settings.mode]}{endpoint}", timeout=TIMEOUT_SECONDS, **kwargs
			)
		except requests.RequestException as exception:
			reason = type(exception).__name__
			self.log_request(endpoint, error=reason)
			raise QikinkUnavailableError(_("Qikink did not answer: {0}").format(reason)) from exception

		self.log_request(
			endpoint, response.status_code, error=None if response.ok else f"HTTP {response.status_code}"
		)
		if response.status_code == HTTPStatus.TOO_MANY_REQUESTS or response.status_code >= 500:
			raise QikinkUnavailableError(_("Qikink is busy or down: HTTP {0}").format(response.status_code))
		if not response.ok:
			raise QikinkRefusedError(
				_("Qikink refused the request: {0}").format(get_error_text(response)), response.status_code
			)
		try:
			return response.json()
		except ValueError as exception:
			raise QikinkUnavailableError(_("Qikink sent a reply that Commera cannot read.")) from exception

	def log_request(self, endpoint: str, status_code: int | None = None, error: str | None = None) -> None:
		"""Log the endpoint and the outcome only: the bodies carry the customer's address and phone."""
		# Not create_request_log(): it commits, and a push must not commit half of its work.
		frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Qikink",
				"is_remote_request": 1,
				"data": frappe.as_json({"endpoint": endpoint}),
				"output": frappe.as_json({"status_code": status_code}),
				"error": error,
				"status": "Failed" if error else "Completed",
			}
		).insert(ignore_permissions=True)


def get_error_text(response: requests.Response) -> str:
	"""The reason Qikink gives: `error` on a create, `message` on the order list."""
	try:
		reply = response.json()
	except ValueError:
		reply = {}
	return reply.get("error") or reply.get("message") or f"HTTP {response.status_code}"
