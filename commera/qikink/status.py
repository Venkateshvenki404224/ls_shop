from typing import NamedTuple

from frappe.utils import cstr


class RemoteStatus(NamedTuple):
	"""What a Qikink status means for the Qikink Order and for the shopper."""

	state: str
	# None leaves the ecommerce status of the Sales Order as it is.
	ecommerce_status: str | None

	@property
	def is_delivery(self) -> bool:
		return self.ecommerce_status == "Delivered"

	@property
	def is_problem(self) -> bool:
		return self.state == "Needs Attention"


# Spelled as Qikink spells them, such as "PIcked Up" and "Partitally Picklisted".
STATUS_GROUPS = (
	(
		(
			"Live",
			"To be Printed",
			"Partitally Picklisted",
			"Partially Printed",
			"Printed",
			"S-Printed",
			"Stitched/QC",
		),
		RemoteStatus("Pushed", "Order Received"),
	),
	(("Dispatch Ready", "Manifested"), RemoteStatus("Pushed", "Preparing for Shipment")),
	(
		(
			"PIcked Up",
			"In-Transit",
			"Out for Delivery",
			"Delivery rescheduled",
			"Not attempted",
			"Consignee unavailable",
			"Misrouted",
			"ODA",
			"OTP not shared",
			"Residence / office closed",
		),
		RemoteStatus("Pushed", "Shipped"),
	),
	(("Delivered", "Self collect"), RemoteStatus("Completed", "Delivered")),
	(("Returned",), RemoteStatus("Completed", "Returned")),
	(("Partially Returned",), RemoteStatus("Completed", "Partially Returned")),
	(("Cancelled",), RemoteStatus("Cancelled", None)),
	(
		(
			"Out Of Stock",
			"Live-OOS",
			"On Hold",
			"Incorrect/incomplete Address",
			"Exception",
			"Lost",
			"Refused to accept",
			"Open delivery refused",
			"Maximum attempts reached",
			"OTP verification cancelled",
			"RTO Initiated",
			"Reverse Pickup Initiated",
		),
		RemoteStatus("Needs Attention", None),
	),
)
STATUS_MAP = {
	status.casefold(): remote_status for statuses, remote_status in STATUS_GROUPS for status in statuses
}


ECOMMERCE_STATUS_RANK = ("Order Received", "Preparing for Shipment", "Shipped", "Delivered")
RETURN_STATUSES = ("Returned", "Partially Returned")


def get_remote_status(qikink_status: str | None) -> RemoteStatus | None:
	"""The meaning of the Qikink status, in any letter case. None for a status Commera does not know."""
	return STATUS_MAP.get(cstr(qikink_status).casefold())


def get_mixed_order_status(qikink_status: str, warehouse_status: str) -> str:
	"""A mixed order is as far as its slower parcel. It is Returned only when both parcels come back."""
	if qikink_status == warehouse_status == "Returned":
		return "Returned"
	if qikink_status in RETURN_STATUSES or warehouse_status in RETURN_STATUSES:
		return "Partially Returned"
	return min(qikink_status, warehouse_status, key=ECOMMERCE_STATUS_RANK.index)
