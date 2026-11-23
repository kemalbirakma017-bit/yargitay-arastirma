import unittest

import bedesten_server as server


class BedestenHelpersTest(unittest.TestCase):
    def test_unit_filter_accepts_general_assemblies(self):
        self.assertEqual(server._unit_filter("Hukuk Genel Kurulu"), ("HGK", "Hukuk Genel Kurulu"))
        self.assertEqual(server._unit_filter("Ceza Genel Kurulu"), ("CGK", "Ceza Genel Kurulu"))

    def test_unit_filter_accepts_chamber_name(self):
        self.assertEqual(server._unit_filter("3. Hukuk Dairesi"), ("H3", "3. Hukuk Dairesi"))

    def test_unit_filter_rejects_unknown_chamber(self):
        with self.assertRaises(ValueError):
            server._unit_filter("99. Hukuk Dairesi")

    def test_date_filter_accepts_valid_date(self):
        self.assertEqual(server._date_value("2026-11-23"), "2026-11-23T00:00:00.000Z")
        self.assertEqual(server._date_value("2026-11-23", end=True), "2026-11-23T23:59:59.999Z")

    def test_date_filter_rejects_impossible_calendar_date(self):
        with self.assertRaises(ValueError):
            server._date_value("2026-02-30")

    def test_search_query_rejects_empty_phrase(self):
        with self.assertRaises(ValueError):
            server._base_query({"aranan_kelime": "   "})

    def test_search_query_keeps_exact_year_and_number_filters(self):
        query = server._base_query({
            "aranan_kelime": "sulh ile elde edilen menfaat",
            "esas_yil": "2025",
            "esas_ilk_sira_no": "2668",
            "karar_yil": "2025",
        })
        self.assertEqual(query["phrase"], "sulh ile elde edilen menfaat")
        self.assertEqual(query["esasNoYil"], 2025)
        self.assertEqual(query["esasNoSira"], 2668)
        self.assertEqual(query["kararNoYil"], 2025)

    def test_decision_row_uses_source_identifier_and_official_url(self):
        row = server._decision_row({
            "documentId": "123456",
            "birimAdi": "Yargıtay 3. Hukuk Dairesi",
            "esasNo": "2025/2668",
            "kararNo": "2025/5244",
            "kararTarihiStr": "2025-12-01",
        })
        self.assertEqual(row["id"], "yargitay:123456")
        self.assertEqual(row["kaynak"], "https://mevzuat.adalet.gov.tr/ictihat/123456")
        self.assertEqual(row["esas_no"], "2025/2668")


if __name__ == "__main__":
    unittest.main()
