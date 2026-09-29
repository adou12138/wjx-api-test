# 2.9.9 创建层级报告[9999001]

> ID: synth-domain

|参数|类型|说明|必需|备注|
|:---    |:------    |--- |-|--------      |
|title	|string	|报告名称	|是	|最长100字符|
|parent_id	|int	|父级目录	|否	|层级归属|
|category	|int	|所属分类	|否	||
|role	|string	|访问角色	|否	||
|file	|string	|附件地址	|否	||
|share	|bool	|是否分享	|否	|默认false|
|notify	|bool	|是否推送通知	|否	|默认false|
|report_id	|int	|报告标识	|否	||
|import_file	|string	|导入来源文件	|否	||
|user_id	|string	|成员账号	|否	||
|items	|array	|批量条目	|否	||

-  举例
```csharp
{
    "title": "月度分析报告",
    "parent_id": 12,
    "category": 3,
    "role": "admin",
    "file": "https://example.com/a.xlsx",
    "share": true,
    "notify": false,
    "report_id": 55,
    "import_file": "batch.csv",
    "user_id": "member01",
    "items": ["a", "b"]
}
```
