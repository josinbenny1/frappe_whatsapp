# Copyright (c) 2025, Shridhar Patil and Contributors
# See license.txt

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_whatsapp.frappe_whatsapp.doctype.bulk_whatsapp_message.bulk_whatsapp_message import (
	_parse_recipient_data,
)

POST = "frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message.make_post_request"
MOBILE = "+971500000001"


class TestBulkWhatsAppMessage(FrappeTestCase):
	def setUp(self):
		# No test may reach the Graph API
		patcher = patch(POST, side_effect=self.fake_post)
		self.post = patcher.start()
		self.addCleanup(patcher.stop)
		self.sent = 0

		suffix = frappe.generate_hash(length=8)
		self.account = frappe.get_doc(
			{
				"doctype": "WhatsApp Account",
				"account_name": f"Test {suffix}",
				"url": "https://graph.example.com/",
				"version": "v22.0",
				"phone_id": suffix,
				"token": "test-token",
			}
		)
		self.account.insert(ignore_permissions=True)

		# db_insert: inserting a template registers it with Meta
		self.template = frappe.new_doc("WhatsApp Templates")
		self.template.update(
			{
				"name": f"test-{suffix}",
				"template_name": f"test_{suffix}",
				"actual_name": f"test_{suffix}",
				"template": "Hello",
				"language": "en",
				"language_code": "en",
				"category": "MARKETING",
			}
		)
		self.template.db_insert()

		self.bulk = frappe.new_doc("Bulk WhatsApp Message")
		self.bulk.update(
			{
				"title": "Test",
				"recipient_type": "Individual",
				"recipient_count": 10,
				"use_template": 1,
				"template": self.template.name,
				"variable_type": "Common",
				"whatsapp_account": self.account.name,
				"status": "Queued",
			}
		)
		self.bulk.autoname()
		self.bulk.db_insert()

	def fake_post(self, *args, **kwargs):
		self.sent += 1
		return {"messages": [{"id": f"wamid.{self.bulk.name}.{self.sent}"}]}

	def messages(self):
		return frappe.get_all(
			"WhatsApp Message",
			filters={"bulk_message_reference": self.bulk.name},
			fields=["status", "to", "body_param"],
		)

	def send(self, recipient_data):
		self.bulk.create_single_message({"mobile_number": MOBILE, "recipient_data": recipient_data})

	def test_parse_recipient_data(self):
		for value in ("", None, "  \n", "{}", "not json", "[1]", "null"):
			with self.subTest(value=value):
				self.assertEqual(_parse_recipient_data(value), {})

		expected = {"customer": "CUST-0001"}
		self.assertEqual(_parse_recipient_data('{"customer":"CUST-0001"}'), expected)
		self.assertEqual(_parse_recipient_data(expected), expected)

	def test_message_is_sent_for_each_recipient_data(self):
		values = ("", None, "{}", '{"customer":"CUST-0001"}', "not json")
		for value in values:
			with self.subTest(value=value):
				before = self.post.call_count
				self.send(value)
				self.assertEqual(self.post.call_count, before + 1)

		self.assertEqual([m.status for m in self.messages()], ["Queued"] * len(values))
		self.assertEqual(self.bulk.get_progress()["failed"], 0)

	def test_recipient_data_fills_template_fields(self):
		self.template.db_set({"sample_values": "x", "field_names": "customer"})
		for variable_type in ("Common", "Unique"):
			with self.subTest(variable_type=variable_type):
				self.bulk.variable_type = variable_type
				self.send('{"customer":"CUST-0001"}')
				data = json.loads(self.post.call_args.kwargs["data"])
				self.assertEqual(
					data["template"]["components"],
					[{"type": "body", "parameters": [{"type": "text", "text": "CUST-0001"}]}],
				)

	def test_url_has_no_double_slash(self):
		self.send("")
		self.assertEqual(
			self.post.call_args.args[0],
			f"https://graph.example.com/v22.0/{self.account.phone_id}/messages",
		)

	def test_failure_is_logged_and_counted_without_resending(self):
		self.post.side_effect = Exception("API down")
		# notify() reads the API error from here
		frappe.flags.integration_request = frappe._dict(json=lambda: {"error": {"message": "API down"}})
		with patch("frappe.log_error") as log_error:
			self.send("")

		self.assertEqual(self.post.call_count, 1)
		logged = log_error.call_args.kwargs["message"]
		self.assertIn(self.bulk.name, logged)
		self.assertIn(MOBILE, logged)
		self.assertNotIn("test-token", logged)

		self.assertEqual([m.status for m in self.messages()], ["Failed"])
		self.assertEqual(self.bulk.get_progress()["failed"], 1)
		self.assertEqual(self.bulk.status, "Partially Failed")

	def test_missing_template_values_and_bad_common_json_are_counted(self):
		self.template.db_set({"sample_values": "x", "field_names": "customer"})
		with patch("frappe.log_error") as log_error:
			self.send("")
			self.bulk.template_variables = "not json"
			self.send("")

		self.assertEqual(log_error.call_count, 2)
		self.assertEqual(self.post.call_count, 0)
		self.assertEqual(self.bulk.get_progress()["failed"], 2)

	def test_non_template_failure_is_counted(self):
		self.bulk.use_template = 0
		self.post.side_effect = Exception("API down")
		frappe.flags.integration_request = frappe._dict(json=lambda: {"error": {"message": "API down"}})
		with patch("frappe.log_error"):
			self.send("")
		self.assertEqual(self.bulk.get_progress()["failed"], 1)
