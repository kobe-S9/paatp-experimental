import os
import glob
import numpy as np

from putil import *

import inap

import argparse


parser = argparse.ArgumentParser(fromfile_prefix_chars='@')
parser.add_argument('-p', '--plot', action='store_true', help='plot and show')
args = parser.parse_args()

#results/scale/20210121-193134/m=5,n=1,z=10000.0,c=10000.0,i=4.out
def parse_inputfile(fn):
    #parse_inputfile 函数读取一个结果文件（路径由 fn 传入），并从文件名中解析出一个字典（d），
    #然后从文件的第一行提取结果。如果提取失败，返回 None。
    dirname = os.path.dirname(fn)
    basename = os.path.basename(fn)
    file_name, file_type = os.path.splitext(basename)

    d = eval('dict(' + file_name + ')')

    with open(fn) as f:
        text = f.readline()
    
    try:
        results = eval(text)
        return d, results
    except:
        return None, None


def calc_perf_metrics(d):
    #该函数处理包含模拟结果的字典 d，计算每个作业的性能指标
    perfm = {}
    for k, v in d.items():
        # k
        # needed time
        for job_id in v:
            dd = perfm.setdefault(k, {}).setdefault(job_id, {})
            start_time = None
            completed_time = -1
            received_ping = 0
            sent_pong = 0
            for f_id in v[job_id]:
                f_info = v[job_id][f_id]
                if start_time is None or start_time > f_info['start_time']:
                    start_time = f_info['start_time']
                if f_info['completed_time'] is not None and completed_time < f_info['completed_time']:
                    completed_time = f_info['completed_time']
                received_ping += f_info['received_ping']
                sent_pong += f_info['sent_pong']

            dd['completion_time'] = completed_time - start_time
            dd['traffic_volume'] = dict(received_ping=received_ping, sent_pong=sent_pong)
    
    return perfm
        

def plot_scale():
    #plot_scale() 函数从 results/scale/*/*.out 文件中读取数据并处理模拟结果，
    #计算如 完成时间 和 流量量 等性能指标。如果 args.plot 被设置为 True，则绘制相关的图表
    #并通过 mkAXS() 函数（可能是自定义的绘图函数）来生成图表。
    #如果 args.plot 被设置，最终调用 show() 来显示图表。
    pdata = {}
    for fn in glob.glob('results/scale/*/*.out'):
        k, v = parse_inputfile(fn)
        if v is None:
            continue
        #print(k, v)
        #break
        #m=5,n=1,z=10000.0,c=10000.0,i=4.out
        key = (k['n'], k['z'], k['c'])
        m = k['m']
        pdata.setdefault(key, {}).setdefault(m, []).append(calc_perf_metrics(v))

    pdata_of_each_method = {} 

    methods = set()
    for k1 in pdata:
        pdata_of_each_method[k1] = {}
        for m in pdata[k1]:
            if m % 10 == 5:
                continue

            pdata_of_each_method[k1][m] = {}
            for perfm in pdata[k1][m]: # list
                for method in perfm:
                    methods.add(method)
                    if method not in pdata_of_each_method[k1][m]:
                        pdata_of_each_method[k1][m][method] = dict(ct=[], received_ping=[], sent_pong=[])
                    for job_id in perfm[method]:
                        ct = perfm[method][job_id]['completion_time']
                        tv = perfm[method][job_id]['traffic_volume']
                        pdata_of_each_method[k1][m][method]['ct'].append(ct)
                        pdata_of_each_method[k1][m][method]['received_ping'].append(tv['received_ping'])
                        pdata_of_each_method[k1][m][method]['sent_pong'].append(tv['sent_pong'])
    
    model_size = 10e3
    for k1 in pdata_of_each_method:
        print('ct')
        print('orded methods', sorted(methods))    
        xx = []
        yy = {}
        yerr = {}
        for m in sorted(pdata_of_each_method[k1]):
            lst = []
            xx.append(m)
            for method in sorted(pdata_of_each_method[k1][m]):
                print('ct', len(pdata_of_each_method[k1][m][method]['ct']))
                r1 = np.mean(pdata_of_each_method[k1][m][method]['ct'])
                r2 = np.std(pdata_of_each_method[k1][m][method]['ct'])
                lst.append(round(r1, 2))
                yy.setdefault(method, []).append(r1)
                yerr.setdefault(method, []).append(r2)

            print(m, lst)
        if args.plot:
            for m1 in ('MDP', 'MUP'):
                ax = mkAXS(m1 + '-scale-ct-' + str(k1))
                #ax.set_title(m1)

                ax.set_ylabel('Completion time (s)')
                ax.set_xlabel('Number of EDs')
                for m2 in (True, False):
                    label, ls, marker = get_label(m1, m2)
                    ax.errorbar(x=xx, y=yy[(m1, m2)], yerr=yerr[(m1, m2)], label=label, ls=ls, marker=marker)
                ax.legend()
                ax.grid(True)

        print('tv')
        xx = []
        yy = {}
        yerr = {}
        for m in sorted(pdata_of_each_method[k1]):
            lst = []
            xx.append(m)
            for method in sorted(pdata_of_each_method[k1][m]):
                d = pdata_of_each_method[k1][m][method]
                if method[0] == 'MDP':
                    r1 = inap.MDP.PING_PKT_SIZE_IN_BITS
                    r2 = inap.MDP.PONG_PKT_SIZE_IN_BITS
                else:
                    r1 = inap.MUP.PING_PKT_SIZE_IN_BITS
                    r2 = inap.MUP.PONG_PKT_SIZE_IN_BITS
                    
                y = [ (a * r1 + b * r2) / max(r1, r2) / model_size 
                        for a, b in zip(d['received_ping'], d['sent_pong'])]
                r1 = np.mean(y)
                print('tv', len(y))
                r2 = np.std(y)
                lst.append(round(r1, 2))
                yy.setdefault(method, []).append(r1)
                yerr.setdefault(method, []).append(r2)

            print(m, lst)

        if args.plot:
            for m1 in ('MDP', 'MUP'):
                ax = mkAXS(m1 + '-scale-tv-' + str(k1))
                #ax.set_title(m1)

                ax.set_ylabel('Traffic load of FLS\n(normalized)')
                ax.set_xlabel('Number of EDs')
                for m2 in (True, False):
                    label, ls, marker = get_label(m1, m2)
                    ax.errorbar(x=xx, y=yy[(m1, m2)], yerr=yerr[(m1, m2)], label=label, ls=ls, marker=marker)

                ax.legend()
                ax.grid(True)
        """
        print('received_ping')
        for m in sorted(pdata_of_each_method[k1]):
            lst = []
            for method in sorted(pdata_of_each_method[k1][m]):
                r = np.mean(pdata_of_each_method[k1][m][method]['received_ping'])
                lst.append(round(r, 2))
                
            print(m, lst)


        print('sent_pong')
        for m in sorted(pdata_of_each_method[k1]):
            lst = []
            for method in sorted(pdata_of_each_method[k1][m]):
                r = np.mean(pdata_of_each_method[k1][m][method]['sent_pong'])
                lst.append(round(r, 2))
                
            print(m, lst)
        """

def get_label(m1, m2):
    # 根据是否包含 "EB"（边缘框）来生成绘图标签。
    if m2:
        return m1 + ' w/   EB', '-', 'x'
    else:
        return m1 + ' w/o EB', '--', '+'


def plot_cachesize():
    #plot_cachesize() 函数处理 results/cachesize/*/*.out 文件，
    #重点在不同缓存大小下的性能。它同样会聚合数据并绘制图表。
    # 并通过 mkAXS() 函数（可能是自定义的绘图函数）来生成图表。
    #如果 args.plot 被设置，最终调用 show() 来显示图表。
    pdata = {}
    for fn in glob.glob('results/cachesize/*/*.out'):
        k, v = parse_inputfile(fn)
        if v is None:
            continue

        #break
        #m=5,n=1,z=10000.0,c=10000.0,i=4.out
        key = (k['m'], k['n'], k['z'])
        c = k['c']
        pdata.setdefault(key, {}).setdefault(c, []).append(calc_perf_metrics(v))

    pdata_of_each_method = {} 

    methods = set()
    
    model_size = 10e3

    for k1 in pdata:
        pdata_of_each_method[k1] = {}
        for x in pdata[k1]:
            if int(x * 10 / model_size) % 2 != 0:
                continue

            pdata_of_each_method[k1][x] = {}
            for perfm in pdata[k1][x]: # list
                for method in perfm:
                    methods.add(method)
                    if method not in pdata_of_each_method[k1][x]:
                        pdata_of_each_method[k1][x][method] = dict(ct=[], received_ping=[], sent_pong=[])
                    for job_id in perfm[method]:
                        ct = perfm[method][job_id]['completion_time']
                        tv = perfm[method][job_id]['traffic_volume']
                        pdata_of_each_method[k1][x][method]['ct'].append(ct)
                        pdata_of_each_method[k1][x][method]['received_ping'].append(tv['received_ping'])
                        pdata_of_each_method[k1][x][method]['sent_pong'].append(tv['sent_pong'])
    
    for k1 in pdata_of_each_method:
        print(k1)
        print('ct')
        print('orded methods', sorted(methods))
        xx = []
        yy = {}
        yerr = {}
        for x in sorted(pdata_of_each_method[k1]):
            lst = []
            for method in sorted(pdata_of_each_method[k1][x]):
                ct = np.mean(pdata_of_each_method[k1][x][method]['ct'])
                lst.append(round(ct, 2))
                yy.setdefault(method, []).append(ct)
                print('ct', len(pdata_of_each_method[k1][x][method]['ct']))
                std = np.std(pdata_of_each_method[k1][x][method]['ct'])
                yerr.setdefault(method, []).append(std)
                
            xx.append(x / model_size)
            print(x, xx[-1], lst)

        if args.plot:
            for m1 in ('MDP', 'MUP'):
                ax = mkAXS(m1 + '-cachesize-ct-' + str(k1))
                #ax.set_title(m1)

                ax.set_ylabel('Completion time (s)')
                ax.set_xlabel('Allocated memory size (normalized)')
                for m2 in (True, False):
                    label, ls, marker = get_label(m1, m2)
                    ax.errorbar(x=xx, y=yy[(m1, m2)], yerr=yerr[(m1, m2)], label=label, ls=ls, marker=marker)
                    print(m1, m2, 'x=', xx, 'y=', yy[(m1, m2)], 'yerr=', yerr[(m1, m2)])
                ax.legend()
                ax.grid(True)

        print('tv')

        #xx = []
        yy = {}
        yerr = {}
        for m in sorted(pdata_of_each_method[k1]):
            lst = []
            for method in sorted(pdata_of_each_method[k1][m]):
                d = pdata_of_each_method[k1][m][method]
                if method[0] == 'MDP':
                    r1 = inap.MDP.PING_PKT_SIZE_IN_BITS
                    r2 = inap.MDP.PONG_PKT_SIZE_IN_BITS
                else:
                    r1 = inap.MUP.PING_PKT_SIZE_IN_BITS
                    r2 = inap.MUP.PONG_PKT_SIZE_IN_BITS
                    
                y = [ (a * r1 + b * r2) / max(r1, r2) / model_size 
                        for a, b in zip(d['received_ping'], d['sent_pong'])]
                r1 = np.mean(y)
                r2 = np.std(y)
                print('tv', len(y))
                yy.setdefault(method, []).append(r1)
                yerr.setdefault(method, []).append(r2)                
                lst.append(round(r1, 2))
            print(m, lst)

        if args.plot:
            for m1 in ('MDP', 'MUP'):
                ax = mkAXS(m1 + '-cachesize-tv-' + str(k1))
                #ax.set_title(m1)
                ax.set_ylabel('Traffic load of FLS\n(normalized)')
                ax.set_xlabel('Allocated memory size (normalized)')
                for m2 in (True, False):
                    label, ls, marker = get_label(m1, m2)
                    ax.errorbar(x=xx, y=yy[(m1, m2)], yerr=yerr[(m1, m2)], label=label, ls=ls, marker=marker)
                    print(m1, m2, 'x=', xx, 'y=', yy[(m1, m2)], 'yerr=', yerr[(m1, m2)])
                    
                ax.legend()
                ax.grid(True)

    """
        print('received_ping')
        for x in sorted(pdata_of_each_method[k1]):
            lst = []
            for method in sorted(pdata_of_each_method[k1][x]):
                r = np.mean(pdata_of_each_method[k1][x][method]['received_ping'])
                lst.append(round(r, 2))
                
            print(x, lst)

        print('sent_pong')
        for m in sorted(pdata_of_each_method[k1]):
            lst = []
            for method in sorted(pdata_of_each_method[k1][m]):
                r = np.mean(pdata_of_each_method[k1][m][method]['sent_pong'])
                lst.append(round(r, 2))
                
            print(m, lst)
    """

if __name__ == '__main__':
    plot_scale()
    plot_cachesize()
    if args.plot:
        show()