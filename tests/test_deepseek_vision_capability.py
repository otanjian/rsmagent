# encoding:utf-8
"""模型图像能力判定必须与该模型的**真实行为**一致。

事故背景：Web 控制台的智能体回答「我收到的只是一个路径字符串，不是图像本身」，
并且反复声称自己无法看图。根因之一是 ``DeepSeekBot.supports_vision`` 把当前配置
的 ``deepseek-v4-flash`` 判为不可读图，从而关闭了视觉工具的主模型通道。

该判定当时依赖一句注释断言（"deepseek-v4-flash 遇图返回 400"）。实测（同一张真实
上传图片、当前 `config.json` 的 DeepSeek 凭据）三个模型名**均返回 HTTP 200 且给出
准确的画面描述**，即断言与其真实行为不符。

这里把能力表钉住：可读图的模型名必须判为真，纯文本模型必须判为假。后者是反向
控制，防止「一律返回 True」这种能让前一条通过的退化。
"""

import unittest
from unittest.mock import patch

import pytest


def _supports_vision(model):
    """真实 DeepSeekBot，只把 SessionManager 换掉（避免构造期副作用）。

    取值必须在补丁生效期内完成：该属性每次读取都会去问当前配置的模型，
    出了 ``with`` 就变回进程真实配置，测不出该模型名对应的判定。
    """
    fake_conf = {"model": model, "deepseek_api_key": "sk-test"}
    with patch("models.deepseek.deepseek_bot.conf", return_value=fake_conf):
        with patch("models.deepseek.deepseek_bot.SessionManager"):
            from models.deepseek.deepseek_bot import DeepSeekBot

            return bool(DeepSeekBot().supports_vision)


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4-flash",             # 事故中实际配置的模型，实测可读图
        "deepseek-flash",
        "deepseek-v4-flash-vision-exp",
    ],
)
def test_multimodal_deepseek_models_advertise_vision(model):
    assert _supports_vision(model), f"{model} should be marked as multimodal"


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-chat",
        "deepseek-reasoner",
        "deepseek-v4-pro",
    ],
)
def test_text_only_deepseek_models_do_not_advertise_vision(model):
    assert not _supports_vision(model), f"{model} should not be marked as multimodal"


class _Sanity(unittest.TestCase):
    def test_constants_used_by_the_contract_still_exist(self):
        """判定集必须以 const 常量为准，避免手写字面量再次漂移。"""
        from common import const

        for name in ("DEEPSEEK_FLASH", "DEEPSEEK_V4_FLASH", "DEEPSEEK_V4_FLASH_VISION_EXP"):
            self.assertTrue(hasattr(const, name), f"const.{name} is missing")


class _CapabilitySourceOfTruth(unittest.TestCase):
    """判定必须依据**这个 bot 实际会发送的模型**，不是别的来源。

    bot 的请求用 ``self.args["model"]``（见 ``call_with_tools``）；全局配置缺失时
    这个值会回退到默认模型。只读全局配置会把「实际用视觉模型的 bot」判成不可读图,
    而那正是本次事故的失败形态：图片被静默地不投递。
    """

    def _bot_without_global_model(self):
        from unittest.mock import patch

        with patch("models.deepseek.deepseek_bot.conf", return_value={"deepseek_api_key": "sk-test"}):
            with patch("models.deepseek.deepseek_bot.SessionManager"):
                from models.deepseek.deepseek_bot import DeepSeekBot

                return DeepSeekBot()

    def test_a_bot_with_no_global_model_still_reports_its_real_capability(self):
        bot = self._bot_without_global_model()

        self.assertEqual(bot.args["model"], "deepseek-flash",
                         "sanity: the bot falls back to the default model")
        self.assertTrue(
            bot.supports_vision,
            "the bot sends deepseek-flash, so it can accept images regardless of "
            "whether global config named a model",
        )

    def test_the_bots_own_model_wins_over_a_later_config_change(self):
        """构造后配置再变，也不得让判定与该 bot 实际发送的模型脱节。"""
        from unittest.mock import patch

        text_only = {"model": "deepseek-v4-pro", "deepseek_api_key": "sk-test"}
        vision = {"model": "deepseek-flash", "deepseek_api_key": "sk-test"}

        with patch("models.deepseek.deepseek_bot.conf", return_value=text_only):
            with patch("models.deepseek.deepseek_bot.SessionManager"):
                from models.deepseek.deepseek_bot import DeepSeekBot

                bot = DeepSeekBot()

        with patch("models.deepseek.deepseek_bot.conf", return_value=vision):
            self.assertFalse(
                bot.supports_vision,
                "this bot will send deepseek-v4-pro, so it must not be treated as "
                "image-capable just because the global config moved on",
            )


class _VisionToolRoutesToMainModel(unittest.TestCase):
    """能力判定修正后，视觉工具的主模型通道必须真的打开。

    只修 ``supports_vision`` 而不钉住下游，无法防止「判定为真但工具仍不路由
    到主模型」这种半修状态；事故里正是这条通道被关闭。
    """

    def test_main_model_provider_is_used_for_a_real_deepseek_v4_flash_bot(self):
        from unittest.mock import patch

        from common import const

        fake_conf = {
            "model": "deepseek-v4-flash",
            "deepseek_api_key": "sk-test",
        }
        with patch("models.deepseek.deepseek_bot.conf", return_value=fake_conf):
            with patch("models.deepseek.deepseek_bot.SessionManager"):
                from models.deepseek.deepseek_bot import DeepSeekBot

                bot = DeepSeekBot()

                class _FakeModel:
                    model = "deepseek-v4-flash"

                    @staticmethod
                    def _resolve_bot_type(_name):
                        return const.DEEPSEEK

                model = _FakeModel()
                model.bot = bot

                with patch("agent.tools.vision.vision.conf", return_value=fake_conf):
                    from agent.tools.vision.vision import (
                        _MAIN_MODEL_PROVIDER_NAME,
                        Vision,
                    )

                    tool = Vision()
                    tool.model = model
                    self.assertTrue(tool._main_bot_supports_vision(bot))
                    providers = tool._resolve_providers()

        self.assertTrue(providers, "vision tool found no provider at all")
        self.assertEqual(
            providers[0].name, _MAIN_MODEL_PROVIDER_NAME,
            "the main model must be the first vision provider once its "
            "capability is reported correctly",
        )


if __name__ == "__main__":
    unittest.main()
