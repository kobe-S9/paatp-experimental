import argparse

parser = argparse.ArgumentParser(description='hello.')
parser.add_argument('--init-sending-rate-pps', type=int, default=10)
##脚本支持一个命令行参数 --init-sending-rate-pps，设置初始发送速率，默认为 10。
args = parser.parse_args()

import matplotlib.pyplot as plt
import numpy as np
##matplotlib 是一个绘图库，用于绘制图表。
##numpy 是一个科学计算库，在处理数组和数据时非常高效。
from core import *
from inp import *

import math

_fig = lambda: plt.figure(figsize=(5, 3.5)).add_subplot(111)
axs = {}
#定义一个匿名函数 _fig，创建指定大小的图表并返回其子图对象。

def mkAXS(key=0):
    ax = _fig()
    axs[key] = ax
    return ax
##mkAXS 创建一个新的绘图对象并存入 axs 字典。

lw = 2
fontsize = 14
M = 7


def show():
    for name, ax in axs.items():
        #ax.legend(loc='best')
        plt.sca(ax)
        ticklines = ax.xaxis.get_ticklines()
        ticklabels = ax.xaxis.get_ticklabels()
        for tk, tkl in zip(ticklines, ticklabels):
            #    tk.set_markersize(12)
            tkl.set_fontsize(14)
        ticklines = ax.yaxis.get_ticklines()
        ticklabels = ax.yaxis.get_ticklabels()
        for tk, tkl in zip(ticklines, ticklabels):
            #    tk.set_markersize(12)
            tkl.set_fontsize(14)
        #plt.legend()#(loc='best')
        plt.tight_layout()
        #plt.savefig('t{0}.pdf'.format(name))

    plt.show()


class Demo(object):
    def __init__(self,
                 DeviceNum=2,
                 JobNum=1,
                 bw_bps=80e6,
                 lat=1e-2,##链路延迟，单位为秒。
                 loss=0,##: 数据包丢失率。
                 FCLS=MDP,
                 MODEL_SIZE_IN_CHUNK=50e3,##模型大小，用于设置总数据包数
                 max_mdp_cache_size=1e4,#缓存大小。
                 max_mup_cache_size=1e4,##缓存大小。
                 enabled_box=True,#是否启用 EdgeBox（边缘设备）。
                 relocation_enabled=False,#
                 queue_lambda=1.,
                 start_times=None):

        self.link_bw_bps = bw_bps
        self.loss_rate = loss
        self.queue_lambda = queue_lambda

        ideal_rtt = lat * 2
        bdp_in_bits = int(bw_bps * ideal_rtt)
        qsize_in_bits = bdp_in_bits * 4

        max_cwnd = qsize_in_bits * 2 / 8 / BYTES_PER_PACKET
        print("max_cwnd:",max_cwnd)
        #raise ValueError
        #print('# queue size', qsize_in_bits)
        #qdisc = SPQ(mq_sizes, ecn_threshold_factors)
        weights = {1: 8, 2: 4, 3: 2, 4: 1}
        mq_sizes = {i: qsize_in_bits for i in weights}
        RedConfigs = {i: dict(MinTh_r=0.3, MaxTh_r=0.7, wq=0.002, UseEcn=False, UseHardDrop=False) for i in mq_sizes}
        #RED算法相关配置
        qdisc = DWRR(mq_sizes, RedConfigs, weights=weights)
        #qdisc：创建一个字典，键为 mq_sizes 中的每个值，值是一个包含 RED（Random Early Detection）算法配置的字典。
        mup_jobs_config = {}
        for i in range(JobNum):
            mup_jobs_config[i] = dict(flownum=DeviceNum)

        self.boxes = [
            EdgeBox(
                jobs_config=mup_jobs_config,
                max_mdp_cache_size=max_mdp_cache_size,  # 1GB
                max_mup_cache_size=max_mup_cache_size,
                enabled=enabled_box,
                relocation_enabled=relocation_enabled,
            ),
        ]

        self.links = [Link(bw_bps, lat, qdisc.copy(), loss) for _ in range(2 * (DeviceNum + 1))]#6个链路，每个链路包含一个 Link 对象。
        self.flows = []
        box = self.boxes[0]
        for i in range(JobNum):
            for j in range(DeviceNum):
                f = FCLS(
                    path=[
                        self.links[2 * j],#0，2
                        box,
                        self.links[-2],#4
                    ],
                    rpath=[
                        self.links[-1],#5
                        box,
                        self.links[2 * j + 1],#1，3
                    ],
                    nic_rate_pps=2 * bw_bps,
                    job_id=i,
                    start_time=start_times[j],  #0, 
                    #expired_time=20,
                    total_chunk_num=MODEL_SIZE_IN_CHUNK,
                    max_cwnd=max_cwnd,
                )
                self.flows.append(f)

            if FCLS == MUP:
                # make mup flow from EB to FLS
                f = EBMUP(
                    path=[
                        self.links[-2],#4
                    ],
                    rpath=[
                        self.links[-1],#5
                    ],
                    nic_rate_pps=2 * bw_bps,
                    job_id=i,
                    start_time=0,
                    #expired_time=100,
                    total_chunk_num=0,#表示总的数据块数量，初始化为 0。可能会在后续动态更新。
                )
                self.flows.append(f)
                box.register_ebmup(job_id=i, f=f)#调用 box 的 register_ebmup 方法，注册此流到某个调度或管理模块。

        self.net = Environment(self.flows, self.links, self.boxes)

    def runsim(self):
        self.net.run()

    def plot(self):#这段代码定义了一个用于绘制网络流量统计数据的 plot 方法。
        #print(self.stat)
        spec_set = set()
        ax_dict = {}
        output = {}
        plot_data = {}
        for f in self.flows:
            print('flow', f.id, 'sent_ping', f.sent_ping, 'received_ping', f.received_ping, 'sent_pong', f.sent_pong, 'received_pong', f.received_pong)

            print('drop stat', f.lost_reason)
            print('expired time', f.expired_time)

            for t, stat in f.stats:  # f.stats是有元组构成的数组，t为时间，stat为流的状态（一个字典，通过get_stat()得到）
                for k, v in stat.items():  # 设置键k对应的值为v.遍历状态字典中的每个键值对
                    if k not in plot_data:#stat 是一个字典，包含流在时间点 t 的各种状态信息（如吞吐量、延迟等）。
                        plot_data[k] = {}
                    if f.id not in plot_data[k]:
                        plot_data[k][f.id] = []
                    if isinstance(v, list):
                        plot_data[k][f.id].extend(v)
                        spec_set.add(k)#用于记录特殊统计项（即值为列表类型的项）。此类项没有时间信息，直接保存为原始值列表。
                    else:  # 只有数值得属性需要添加时间信息
                        plot_data[k][f.id].append((t, v))
        for k in plot_data:#绘图，每个统计项 k 生成一张单独的图。
            #if 'ecn' in k:
            #    continue
            #if k not in spec_set:
            ax = mkAXS()
            ax.set_title(k)
            for fid in plot_data[k]:
                x, y = [], []  # x为时间，y为数值
                for xx, yy in plot_data[k][fid]:
                    x.append(xx)
                    y.append(yy)
                ax.errorbar(x=x, y=y, label='flow {0}'.format(fid))
            ax.legend()


def convert_to_rate(xy, stepsize=10):#该函数 convert_to_rate 的作用是将累积量数据转换为速率数据，适用于分析随时间累积的统计数据（例如流量、计数器值等）。
    x = []
    y = []
    last_xx, last_yy = xy[0]
    for i, (xx, yy) in enumerate(xy):#enumerate() 是 Python 内置的一个函数，用于同时获取可迭代对象（如列表、元组、字符串等）中的元素及其对应的索引。
        if i > 0 and i % stepsize == 0:
            x.append(xx)
            y.append((yy - last_yy) / (xx - last_xx))
            last_xx = xx
            last_yy = yy
    return x, y
#函数应用场景：
#网络流量分析：
#从累计数据包数或字节数计算带宽（速率）。
#性能监控：
#计算系统的每秒操作数（如每秒请求数、每秒写入量等）。
#数据分析：
#从时间序列累积量数据派生出速率变化。

if __name__ == '__main__':
    for FCLS in (MUP, ):  #MUP
        s = 1
        #a = 0.1
        max_mdp_cache_size = 1e4
        DeviceNum = 2
        for relocation_enabled in (True, False)[:1]:
            inst = Demo(
                DeviceNum=DeviceNum,
                JobNum=1,
                FCLS=FCLS,
                MODEL_SIZE_IN_CHUNK=s,
                max_mdp_cache_size=max_mdp_cache_size,
                max_mup_cache_size=1e4,
                enabled_box=True,  #enabled_box,
                relocation_enabled=relocation_enabled,
                start_times=[0, 1, 20, 0.2])
            inst.runsim()

            print()
            #print('#', FCLS.__name__, 'Box', enabled_box)
            print(inst.boxes[0].mdp_perf_metrics)
            print(inst.boxes[0].mup_perf_metrics)
            print(inst.boxes[0].perf_metrics)
            #print(inst.stat)

            for f in inst.flows:
                print('# Flow', f.__class__.__name__, f.id, "sent ping, received ping, sent pong, recevied pong", f.sent_ping, f.received_ping, f.sent_pong,
                      f.received_pong, f.last_pong_received_time)
            inst.plot()
        show()

    print('done!')
    ###
        #为不同的调度策略（如 MDP）和不同的配置（如启用/禁用重定位）运行模拟。
        #在模拟运行后，打印相关设备的性能指标。
        #输出每个流的状态（包括发送和接收的 ping/pong 包数）。
        #绘制图形以可视化性能数据。
        #最后输出 'done!'，表示模拟完成。
    ###     #