#!/usr/bin/env python3
"""
诊断为什么只有 18 个周期被处理
"""
import os
from pathlib import Path
import pandas as pd

print("=" * 80)
print("🔍 诊断回测周期数问题")
print("=" * 80)

# 1. 检查输入数据
print("\n1️⃣ 检查输入数据目录")
print("-" * 80)

input_dir = Path("artifacts/live/record_job/More_RawData")
cycle_dirs = sorted([d for d in input_dir.iterdir() if d.is_dir() and "btc-updown-5m" in d.name])

print(f"✓ More_RawData 中的周期数: {len(cycle_dirs)}")
print(f"  第 1 个: {cycle_dirs[0].name}")
print(f"  第 18 个: {cycle_dirs[17].name}")
print(f"  第 19 个: {cycle_dirs[18].name}")
print(f"  最后一个: {cycle_dirs[-1].name}")

# 2. 检查回测输出
print("\n2️⃣ 检查回测输出数据")
print("-" * 80)

output_dir = Path("artifacts/live/record_job/batch_replay_outputs_improved_large")

# 检查 cycles.csv
cycles_file = output_dir / "cycles.csv"
if cycles_file.exists():
    df_cycles = pd.read_csv(cycles_file)
    print(f"✓ cycles.csv 中的周期数: {len(df_cycles)}")
    print(f"  第 1 个周期: {df_cycles.iloc[0]['cycle_id']}")
    print(f"  最后一个周期: {df_cycles.iloc[-1]['cycle_id']}")
    
    # 找出对应的索引
    expected_18_cycle = cycle_dirs[17].name
    expected_19_cycle = cycle_dirs[18].name
    
    last_actual = df_cycles.iloc[-1]['cycle_id']
    
    if last_actual == expected_18_cycle:
        print(f"\n✓ 确认: 回测只处理到第 18 个周期")
        print(f"  停止周期: {last_actual}")
        print(f"  下一个未处理: {expected_19_cycle}")
    else:
        print(f"\n⚠️ 周期对不上:")
        print(f"  最后一个周期: {last_actual}")
        print(f"  期望第 18 个: {expected_18_cycle}")

# 3. 检查 metrics.csv
print("\n3️⃣ 检查 metrics.csv")
print("-" * 80)

metrics_file = output_dir / "metrics.csv"
if metrics_file.exists():
    df_metrics = pd.read_csv(metrics_file)
    total_cycles = df_metrics.iloc[0]['total_cycles']
    print(f"✓ Metrics 中记录的总周期数: {int(total_cycles)}")

# 4. 检查是否有错误日志或标志文件
print("\n4️⃣ 检查输出目录中的文件")
print("-" * 80)

for file in sorted(output_dir.glob("*")):
    if file.is_file():
        size_kb = file.stat().st_size / 1024
        if size_kb > 1024:
            size_str = f"{size_kb / 1024:.1f} MB"
        else:
            size_str = f"{size_kb:.1f} KB"
        print(f"  {file.name}: {size_str}")

# 5. 分析可能的原因
print("\n5️⃣ 可能的原因")
print("-" * 80)

print("""
假设场景:
  ✓ More_RawData 中有 91 个周期
  ✓ 指定了 --max-cycles 90
  ✗ 实际只处理了 18 个周期

可能原因 (按概率排序):
  1. 回测脚本在第 18 个周期后出现异常/错误
     → 检查标准错误输出或日志文件
  
  2. daily_limits 模块可能在第 18 个周期触发了日损失限制
     → 日限制达到 100%，暂停了交易
  
  3. 某个规则或计算出现了 NaN/Error，导致回测中止
     → 检查策略日志
  
  4. 命令行参数解析问题
     → 检查实际运行的参数是否正确

建议:
  □ 重新运行回测，记录详细的日志输出
  □ 检查是否有异常堆栈跟踪
  □ 尝试逐个处理周期来定位问题
  □ 检查第 18 和第 19 个周期的数据是否完整
""")
