"""投递层：问数结果的投递事件形状与终态解析规则。

本包只放纯领域逻辑，不做数据库或网络 I/O。持久化在
``lingxi.adapters.postgres_conversation``（outbox 与 task 共用同一张表族与同一个
事务边界，因此没有拆出单独的 ``adapters/postgres_delivery.py``）；应用装配在
``lingxi.apps.worker.service``。
"""
