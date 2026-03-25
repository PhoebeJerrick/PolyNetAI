#!/usr/bin/env python3
"""
正确运行 90 周期回测，并详细记录进度
"""
import subprocess
import os
from pathlib import Path

INPUT_DIR = "artifacts/live/record_job/More_RawData"
OUTPUT_DIR = "artifacts/live/record_job/batch_replay_outputs_improved_large_v2"
CONFIG = "configs/strategy.yaml"

# 确保输出目录存在
os.makedirs(OUTPUT_DIR, exist_ok=True)

print("=" * 80)
print("🚀 运行 90 周期完整回测")
print("=" * 80)
print(f"\n输入数据: {INPUT_DIR}")
print(f"输出目录: {OUTPUT_DIR}")
print(f"配置文件: {CONFIG}\n")

# 列出输入目录中的周期数
input_path = Path(INPUT_DIR)
cycle_dirs = sorted([d for d in input_path.iterdir() if d.is_dir() and "btc-updown-5m" in d.name])
print(f"📊 More_RawData 中发现 {len(cycle_dirs)} 个周期文件夹")
print(f"   范围: {cycle_dirs[0].name} → {cycle_dirs[-1].name}\n")

# 运行回测
cmd = [
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

print(f"执行命令:\n{' '.join(cmd)}\n")
print("等待回测完成...\n")

try:
    result = subprocess.run(cmd, capture_output=False, text=True, timeout=600)
    
    if result.returncode == 0:
        print("\n✅ 回测完成！\n")
        
        # 检查输出文件
        cycles_file = Path(OUTPUT_DIR) / "cycles.csv"
        if cycles_file.exists():
            with open(cycles_file, 'r') as f:
                lines = f.readlines()
            
            data_rows = len(lines) - 1  # 减去 header
            print(f"📊 输出结果:")
            print(f"   cycles.csv: {data_rows} 行周期数据")
            print(f"   文件大小: {cycles_file.stat().st_size / 1024:.1f} KB")
            
            if data_rows < 80:
                print(f"\n⚠️  警告: 仅处理了 {data_rows} 个周期，少于预期的 90 个")
                print(f"   这意味着回测器在运行时提前停止了")
            else:
                print(f"\n✅ 成功处理了预期的周期数")
        
        # 列出所有输出文件
        print(f"\n📁 输出文件列表:")
        for file in sorted(Path(OUTPUT_DIR).glob("*")):
            if file.is_file():
                size_kb = file.stat().st_size / 1024
                if size_kb > 1024:
                    print(f"   {file.name}: {size_kb / 1024:.1f} MB")
                else:
                    print(f"   {file.name}: {size_kb:.1f} KB")
    else:
        print(f"❌ 回测失败，返回码: {result.returncode}")

except subprocess.TimeoutExpired:
    print("❌ 回测超时（600秒）")
except Exception as e:
    print(f"❌ 错误: {e}")
