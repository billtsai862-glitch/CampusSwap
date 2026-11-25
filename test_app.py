import tempfile
import unittest
from pathlib import Path

import app as site


class CampusSwapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        site.DB_PATH = Path(self.temp.name) / "test.db"
        site.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        site.init_db()
        self.client = site.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_homepage(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_register_login_and_logout(self):
        r = self.client.post("/register", data={"username": "student1", "password": "123456"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/login", data={"username": "student1", "password": "123456"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"student1", r.data)
        r = self.client.get("/logout", follow_redirects=True)
        self.assertEqual(r.status_code, 200)

    def test_duplicate_registration(self):
        self.client.post("/register", data={"username": "student1", "password": "123456"})
        r = self.client.post("/register", data={"username": "student1", "password": "abcdef"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("這個帳號已經有人使用".encode(), r.data)

    def test_product_create_detail_and_search(self):
        self.client.post("/register", data={"username": "student2", "password": "123456"})
        self.client.post("/login", data={"username": "student2", "password": "123456"})
        r = self.client.post("/product/new", data={
            "title": "微積分課本", "category": "課本參考書", "price": "150",
            "condition_text": "保存良好", "description": "少量筆記"
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("微積分課本".encode(), r.data)
        r = self.client.get("/search?q=微積分")
        self.assertEqual(r.status_code, 200)
        self.assertIn("微積分課本".encode(), r.data)
        r = self.client.get("/product/1")
        self.assertEqual(r.status_code, 200)

    def test_listing_requires_login(self):
        r = self.client.get("/product/new", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("登入".encode(), r.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
