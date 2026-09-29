# 创建 AI 主页

## 接口地址
https://example.test/openapi/default.aspx

## 请求参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| action | int | 是 | 动作标识 1000107 |
| encode | string | 是 | sha1 |
| appid | int | 是 | 应用 ID |
| sign | string | 是 | 签名 |
| ts | int | 否 | 时间戳 |
| title | string | 是 | 主页标题，长度不超过 100 个字符 |
| html | string | 否 | 主页内容 HTML，可为空 |
| status | int | 否 | 状态：0=草稿，1=发布，默认值为 0 |

## 成功响应

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| result | bool | 是 | 是否成功 |
| homepage_id | string | 是 | AI 主页 ID |
| homepage_url | string | 否 | 主页地址 |
| status | int | 否 | 当前状态 |

## 失败响应

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| result | bool | 是 | 是否成功 |
| code | string | 否 | 错误码 |
| message | string | 否 | 错误信息 |
