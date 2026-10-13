"""ЗӨВХӨН ТЕСТЭД: хүний гар нэвтрэлтийг дуурайх hook.

Production код хэзээ ч нэвтрэх талбар бөглөдөггүй — хэрэглэгч нээгдсэн хөтөч дээр өөрөө
нэвтэрнэ. Автомат тестэд тэр "хүн"-ийг энэ hook орлоно: PipelineRunner гар нэвтрэлт хүлээж
эхлэхэд (`on_waiting_login`) нээлттэй page дээр mock сайтын формыг бөглөнө.
"""
from __future__ import annotations

import asyncio


class HumanLogin:
    def __init__(self, user: str, password: str, company: str = "", delay_s: float = 0.3,
                 enabled: bool = True):
        self.user = user
        self.password = password
        self.company = company
        self.delay_s = delay_s
        self.enabled = enabled        # False → "хүн" нэвтрэхгүй (хугацаа хэтрэх тохиолдол)
        self.calls = 0

    async def __call__(self, page) -> None:
        self.calls += 1
        if not self.enabled:
            return
        await asyncio.sleep(self.delay_s)          # хүн хөтөч рүү шилжих хугацаа
        await page.wait_for_selector("input[type=password]", timeout=10000)
        if self.company and await page.locator("input[name=company]").count():
            await page.fill("input[name=company]", self.company)
        await page.fill("input[name=user]", self.user)
        await page.fill("input[type=password]", self.password)
        await page.click("button[type=submit]")
