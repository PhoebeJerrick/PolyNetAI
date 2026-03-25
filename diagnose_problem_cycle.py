#!/usr/bin/env python3
"""
逐周期诊断 - 找出具体是哪个周期导致回测停止
"""
from pathlib import Path
import json

INPUT_DIR = Path("artifacts/live/record_job/More_RawData")
GOOD_OUTPUT = Path("artifacts/live/record_job/batch_replay_outputs_improved_large")
CYCLES_CSV = GOOD_OUTPUT / "cycles.csv"

print("=" * 80)
print("🔍 逐周期诊断 - 找出问题周期")
print("=" * 80)

# 1. 读取已成功处理的周期
print("\n1️⃣ 已成功处理的周期:")
print("-" * 80)

if CYCLES_CSV.exists():
    successful_cycles = []
    with open(CYCLES_CSV, 'r') as f:
        lines = f.readlines()
    
    # 跳过 header，读取所有周期
    for i, line in enumerate(lines[1:], 1):
        parts = line.split(',')
        if len(parts) > 1:
            cycle_id = parts[1]  # cycle_id 在第 2 列
            successful_cycles.append(cycle_id)
    
    print(f"✓ 成功处理了 {len(successful_cycles)} 个周期")
    print(f"  第 1 个: {successful_cycles[0]}")
    print(f"  最后一个: {successful_cycles[-1]}")
    
    # 2. 找出所有可用的周期
    print("\n2️⃣ 所有可用的周期:")
    print("-" * 80)
    
    all_cycles = sorted([d.name for d in INPUT_DIR.iterdir() if d.is_dir() and "btc-updown-5m" in d.name])
    print(f"✓ 总共有 {len(all_cycles)} 个周期")
    print(f"  第 1 个: {all_cycles[0]}")
    print(f"  第 19 个: {all_cycles[18]}")  # 第 19 个（因为是 0-index）
    print(f"  最后一个: {all_cycles[-1]}")
    
    # 3. 比较找出问题周期
    print("\n3️⃣ 问题周期分析:")
    print("-" * 80)
    
    last_successful = successful_cycles[-1]
    
    try:
        # 找出对应的索引
        last_idx = all_cycles.index(last_successful)
        
        print(f"✓ 最后成功处理的周期: {last_successful}")
        print(f"  在列表中的位置: 第 {last_idx + 1} 个")
        
        if last_idx + 1 < len(all_cycles):
            problem_cycle = all_cycles[last_idx + 1]
            print(f"\n⚠️  下一个周期（导致问题）: {problem_cycle}")
            print(f"  在列表中的位置: 第 {last_idx + 2} 个")
            
            # 检查这个周期的数据文件
            problem_dir = INPUT_DIR / problem_cycle
            ws_file = problem_dir / "ws_trade_events.ndjson"
            
            if ws_file.exists():
                file_size = ws_file.stat().st_size
                with open(ws_file, 'r') as f:
                    line_count = sum(1 for _ in f)
                
                print(f"\n  数据文件: {ws_file.name}")
                print(f"  文件大小: {file_size / 1024:.1f} KB")
                print(f"  事件行数: {line_count}")
                
                # 尝试检查文件内容是否有效
                print(f"\n  检查文件格式...")
                try:
                    with open(ws_file, 'r') as f:
                        for i, line in enumerate(f):
                            if i < 3:  # 检查前 3 行
                                try:
                                    json.loads(line)
                                    print(f"    行 {i + 1}: ✓ 有效 JSON")
                                except:
                                    print(f"    行 {i + 1}: ✗ 无效 JSON - {line[:80]}")
                except Exception as e:
                    print(f"  错误: {e}")
            else:
                print(f"\n  ⚠️ 数据文件不存在: {ws_file}")
    
    except ValueError:
        print(f"✗ 最后成功周期在列表中未找到: {last_successful}")

print("\n" + "=" * 80)
print("建议:")
print("-" * 80)
print("""
如果发现第 19 个周期的数据有问题:
  1. 尝试跳过这个周期，继续处理剩余的
  2. 或者使用 --max-cycles 参数，设置为更小的值来避开问题周期
  
例如，如果第 19 个周期有问题，可以：
  python scripts/run_recorded_live_paper.py \\
    --input-dir=artifacts/live/record_job/More_RawData \\
    --cycle-glob=btc-updown-5m-* \\
    --max-cycles=18 \\
    ... 其他参数 ...
    
然后再继续处理剩余的周期。
""")
