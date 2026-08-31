import unittest


class CityCodeTests(unittest.TestCase):
    def test_guangzhou_and_shenzhen_use_boss_city_codes(self):
        from jobwinner.config import CITY_CODES

        self.assertEqual(CITY_CODES["广州"], "101280100")
        self.assertEqual(CITY_CODES["深圳"], "101280600")

    def test_custom_city_code_overrides_builtin_mapping(self):
        from jobwinner.channels import get_channel
        from jobwinner.scraper.jobs import _resolve_city_code

        config = {"search": {"city_codes": {"北京": "custom-code", "自定义城市": 123}}}

        # BOSS 适配器没有自己的城市码表 → 配置里的自定义码生效。
        channel = get_channel("bosszp")
        self.assertEqual(_resolve_city_code("北京", config, channel), "custom-code")
        self.assertEqual(_resolve_city_code("自定义城市", config, channel), "123")


if __name__ == "__main__":
    unittest.main()
