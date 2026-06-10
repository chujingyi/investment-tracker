import re

# 定义正则表达式来匹配库存变化的记录
pattern = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - 商品: .*库存变化: \d+ -> \d+ \(差值: (-?\d+)\)")

# 用于存储提取的结果
inventory_changes = []

# 尝试用不同的编码读取日志文件
encodings = ["utf-8", "gbk", "latin1"]  # 常见的编码列表

for encoding in encodings:
    try:
        with open("iminitv.log", "r", encoding=encoding) as file:
            for line in file:
                match = pattern.search(line)
                if match:
                    # 提取时间和差值
                    time_str = match.group(1)
                    change = int(match.group(2))
                    inventory_changes.append((time_str, change))
        break  # 如果成功读取，跳出循环
    except UnicodeDecodeError:
        continue  # 如果当前编码失败，尝试下一个编码
else:
    raise Exception("无法解码文件，请检查文件编码")

# 按库存变化值从小到大排序
inventory_changes.sort(key=lambda x: x[1])

# 将结果写入 mb.log 文件
with open("mb.log", "w", encoding="utf-8") as output_file:
    for time_str, change in inventory_changes:
        # 格式化为指定格式
        output_file.write(f"{time_str} 库存变化：{change}\n")

print("处理完成，结果已保存到 mb.log")