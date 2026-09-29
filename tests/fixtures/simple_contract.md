# 创建示例资源

## 接口地址
https://example.test/api/resources

## 请求参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| action | int | 是 | 动作标识 |
| title | string | 是 | 资源标题，长度不超过 20 个字符 |
| description | string | 否 | 资源描述，可为空 |
| status | int | 否 | 状态：0=草稿，1=发布，默认值为 0 |

## 成功响应

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| result | bool | 是 | 是否成功 |
| resource_id | string | 是 | 资源 ID |
| title | string | 是 | 标题 |

## 失败响应

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| result | bool | 是 | 是否成功 |
| code | string | 否 | 错误码 |
| message | string | 否 | 错误信息 |
