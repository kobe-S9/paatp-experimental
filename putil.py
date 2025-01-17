import os
import glob
import argparse

#from matplotlib import rcParams
#rcParams['font.family'] = 'sans-serif'
#rcParams['font.sans-serif'] = ['Tahoma']
#rcParams['font.family'] = 'serif'
#rcParams['font.serif'] = 'Times New Roman'

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import FormatStrFormatter, PercentFormatter #用于控制坐标轴刻度的显示方式



def calc_yerr(y_lst):
    #计算给定数据列表的标准差，并将其作为误差返回。它用来绘制带误差条的图形。
    v = np.std(y_lst)
    return v, v

#def _fig(w=5, h=3.5):
#    return plt.figure(figsize=(w, h)).add_subplot(111)

def _fig(w=3.5, h=2.5): #4
    #创建一个指定宽高的图形对象。w 和 h 为图形的宽度和高度。
    return plt.figure(figsize=(w, h)).add_subplot(111)

axes_map = {}

def mkAXS(key=0, **kwargs):
    #生成一个绘图轴（ax），并将其与一个关键字 key 关联，存储到 axes_map 中。
    #key 作为图形的名字，用于区分不同的绘图。
    ax = _fig(**kwargs)
    key = key.replace('.', '_').replace('(', '').replace(')', '').replace(' ', '')
    axes_map[key] = ax
    return ax


def show():
    #显示所有生成的图形。如果图形已经存储在 axes_map 中，它会保存为 PDF 文件并显示。
    #plt.show()
    #return
    for name, ax in axes_map.items():
        #ax.legend()
        plt.sca(ax)
        ticklines = ax.xaxis.get_ticklines()
        ticklabels = ax.xaxis.get_ticklabels()
        for tk, tkl in zip(ticklines, ticklabels):
            #    tk.set_markersize(12)
            #tkl.set_fontsize(11)
            pass
        ticklines = ax.yaxis.get_ticklines()
        ticklabels = ax.yaxis.get_ticklabels()
        for tk, tkl in zip(ticklines, ticklabels):
            #    tk.set_markersize(12)
            #tkl.set_fontsize(11)
            pass
        plt.tight_layout()
        plt.savefig('{0}.pdf'.format(name))
    plt.show()


def parse_inputfile(dirpath, filename='outputfile.txt'):
    #解析指定目录下的 outputfile.txt 文件，提取数据并将其以字典形式返回。
    #文件名中的一些数据（如 method）会被分组存储。
    s = os.path.basename(dirpath)
    d = eval('dict(' + s + ')')
    file_name = os.sep.join([dirpath, filename])
    rets = {}
    with open(file_name) as f:
        for line in f.readlines():
            line = line.strip()
            if line.startswith('#'):
                continue
            if 0 == len(line):
                continue
            it = eval(line)
            rets.setdefault(it['method'], []).append(it)
    return d, rets

#遍历指定路径下的所有目录，读取每个 outputfile.txt 文件，解析并合并其中的数据。这两个函数的区别在于：
#parse_and_merge_results_old 会将 method、m、n 等多个参数作为键，将对应的实验结果保存为列表。
#parse_and_merge_results 会将实验结果直接合并到一个字典中。
def parse_and_merge_results_old(path):
    results = {}
    ts = {}
    for (dirpath, dirnames, filenames) in os.walk(path):
        for filename in filenames:
            if filename != 'outputfile.txt': 
                continue
            d, rets = parse_inputfile(dirpath, filename)
            #m={m},n={n},sr={sharing_ratio},lr={load_ratio},sc={server_cap},i={i}
            k = (d['m'], d['n'], d['lr'], d['sr'], d['sc'])
            # 'cost': (3235.0, 3235.0, 0), 
            for method in rets:
                results.setdefault(method, {}).setdefault(k, []).append(rets[method]['detail'])
                ts.setdefault(method, {}).setdefault(k, []).append(rets[method]['time'])
    return ts, results


def parse_and_merge_results(path):
    results = {}
    for (dirpath, dirnames, filenames) in os.walk(path):
        for filename in filenames:
            if filename != 'outputfile.txt': 
                continue
            d, rets = parse_inputfile(dirpath, filename)
            #m={m},n={n},sr={sharing_ratio},lr={load_ratio},sc={server_cap},i={i}
            k = (d['m'], d['n'], d['lr'], d['sr'], d['sc'])
            # 'cost': (3235.0, 3235.0, 0), 
            for method in rets:
                results.setdefault(method, {}).setdefault(k, []).extend(rets[method])
    return results
