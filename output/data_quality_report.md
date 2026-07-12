# 数据技术完整性检查报告

- 检查范围：54 个 matrix JSON 与 index.json
- 年份要求：至少包含 2018 年及以后年份
- index.json：已解析
- fatal：0
- warning：0
- info：0

## 规则

- fatal：文件无法解析、基础结构缺失、或 index 引用无法对应真实 matrix。
- warning：年份断档、字段为空、非数字值，或年份格式异常。
- info：指标行较少；不对 USDA 数值或商品天然缺失指标作判断。
