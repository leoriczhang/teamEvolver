# 直接登记当前分配

来源提取不需要读此附件。读取assignment.json开头80行取得一个当前真实输入ID后，直接调用
index_assignment；offset和limit按行，不按字符。不分页、不复制全部ID、不执行旧正文。
脚本验证锚点属于唯一当前分配后处理其全部输入。原始文件完整登记，旧Markdown只作路径对照，
旧配置和分析返回空records。直接交付record_response_ref，不手写记录或再次读取。
