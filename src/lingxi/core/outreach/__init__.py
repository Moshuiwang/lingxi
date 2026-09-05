"""主动发送能力：组卡纯逻辑与发送编排。

与预开通解耦——给定「收件人 + 一张卡片」就能发出去并留下可回查的记录，预开通
完成后的告知只是第一个调用方。本包不 import 任何外部 SDK，也不碰数据库：真实
出站在 ``adapters/feishu_user_card.py``，持久记录在
``adapters/postgres_outreach.py``，装配在 ``scripts/ops/outreach.py``。
"""
