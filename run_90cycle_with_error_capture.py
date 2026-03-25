#!/usr/bin/env python3
"""
改进的 90 周期回测脚本 - 带详细错误处理和进度跟踪
"""
import subprocess
import os
from pathlib import Path
import time
import sys

INPUT_DIR = "artifacts/live/record_job/More_RawData"
OUTPUT_DIR = "artifacts/live/record_job/batch_replay_outputs_improved_large_fixed"
CONFIG = "configs/strategy.yaml"

os.makedirs(OUTPUT_DIR, exist_ok=True)

print("=" * 80)
print("🚀 运行 90 周期完整回测 (改进版)")
print("=" * 80)

# 列出所有周期
input_path = Path(INPUT_DIR)
cycle_dirs = sorted([d for d in input_path.iterdir() if d.is_dir() and "btc-updown-5m" in d.name])
total_cycles = len(cycle_dirs)

print(f"\n📊 More_RawData 中发现 {total_cycles} 个周期")
print(f"   范围: {cycle_dirs[0].name} → {cycle_dirs[-1].name}\n")

# 方案 A: 直接运行 90 周期 (使用原始命令)
print("━" * 80)
print("方案 A: 直接运行 90 个周期")
print("━" * 80)

cmd_90 = [
    "python",
    "scripts/run_recorded_live_paper.py",
    f"--input-dir={INPUT_DIR}",
    "--cycle-glob=btc-updown-5m-*",
    "--max-cycles=90",
    f"--config={CONFIG}",
    f"--output-dir={OUTPUT_DIR}",
    "--pace-factor=20",
    "--status-every=100",
    "--dashboard-refresh-seconds=1",
    "--starting-cash=1000"
]

print(f"\n执行: {' '.join(cmd_90[:5])} ...\n")

try:
    start_time = time.time()
    result_90 = subprocess.run(cmd_90, timeout=1200, capture_output=True, text=True)
    elapsed = time.time() - start_time
    
    print(f"✓ 回测完成 (耗时: {elapsed:.1f} 秒)")
    
    # 检查输出
    cycles_file = Path(OUTPUT_DIR) / "cycles.csv"
    if cycles_file.exists():
        with open(cycles_file, 'r') as f:
            lines = f.readlines()
        actual_cycles = len(lines) - 1
        
        print(f"✓ 处理了 {actual_cycles} 个周期")
        
        if actual_cycles < 80:
            print(f"\n⚠️  警告: 仅处理了 {actual_cycles} 个周期，少于预期的 90 个")
            print(f"\n详细调查:")
            
            # 打印标准错误输出
            if result_90.stderr:
                print(f"\n错误输出:")
                print(result_90.stderr[:2000])
            
            # 检查是否有异常信息
            if "Error" in result_90.stdout or "Exception" in result_90.stdout:
                print(f"\n标准输出中的错误提示:")
                for line in result_90.stdout.split('\n'):
                    if 'Error' in line or 'Exception' in line or 'Traceback' in line:
                        print(f"  {line}")
        else:
            print(f"\n✅ 成功处理了 {actual_cycles} 个周期！")
    
    # 列出输出文件
    print(f"\n📁 生成的文件:")
    for file in sorted(Path(OUTPUT_DIR).glob("*")):
        if file.is_file():
            size_kb = file.stat().st_size / 1024
            if size_kb > 1024:
                print(f"   {file.name}: {size_kb / 1024:.1f} MB")
            else:
                print(f"   {file.name}: {size_kb:.1f} KB")
    
except subprocess.TimeoutExpired:
    print("❌ 回测超时（超过 20 分钟）")
except Exception as e:
    print(f"❌ 错误: {e}")
    if 'result_90' in locals() and result_90.stderr:
        print(f"\n错误详情:\n{result_90.stderr}")

print("\n" + "=" * 80)
print("通过 cycles.csv 中的周期数来确定问题所在")
print("=" * 80)
