import frappe
import requests
from frappe.utils import cint, get_request_session

BASE_URLS = {"sandbox": "https://sandbox.qikink.com", "live": "https://api.qikink.com"}
TIMEOUT_SECONDS = 30
TOKEN_EXPIRY_MARGIN_SECONDS = 60


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

	def create_order(self, body: dict) -> dict:
		headers = {"ClientId": self.settings.client_id, "Accesstoken": self.get_access_token()}
		return self.send("POST", "/api/order/create", headers=headers, json=body)

	def send(self, method: str, endpoint: str, **kwargs) -> dict:
		try:
			response = get_request_session().request(
				method, f"{BASE_URLS[self.settings.mode]}{endpoint}", timeout=TIMEOUT_SECONDS, **kwargs
			)
		except requests.RequestException as exception:
			self.log_request(endpoint, error=type(exception).__name__)
			raise

		self.log_request(
			endpoint, response.status_code, error=None if response.ok else f"HTTP {response.status_code}"
		)
		response.raise_for_status()
		return response.json()

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
