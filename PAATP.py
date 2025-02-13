import argparse
import matplotlib.pyplot as plt
import numpy as np
from core import *
import math
import copy

from collections import OrderedDict

parser = argparse.ArgumentParser(description='hello.')
parser.add_argument('--init-sending-rate-pps', type=int, default=10)
parser.add_argument('--num', type=int, required=False, default=0,help='gradient_num')
parser.add_argument('--jobnum', type=int, required=False, default=0,help='jobnum')
args = parser.parse_args()


_fig = lambda: plt.figure(figsize=(5, 3.5)).add_subplot(111)
axs = {}

lw = 2
fontsize = 14
M = 7


class PingPongFlow(Flow):
    CC_STATE_SLOW_START = 1
    CC_STATE_CONGESTION_AVOIDANCE = 2
    CC_STATE_FAST_RECOVERY = 3

    PING_PKT_TYPE = 'PING'
    PONG_PKT_TYPE = 'PONG'
    CWD_PKT_TYPE = 'CWD'
    ACEN_PKT_TYPE = 'ACEN'

    PING_PKT_SIZE_IN_BITS = 2400 #300bytes
    PONG_PKT_SIZE_IN_BITS = 2400 #300bytes
    CWD_PKT_SIZE_IN_BITS = 496 #62bytes
    ACEN_PKT_SIZE_IN_BITS = PONG_PKT_SIZE_IN_BITS

    DUPLICATED_PONG_THRESHOLD = 3

    def start(self):
        super().start()
        self.job_id = self.params.get('job_id', 0)

        self.rate_bps = self.params.get('nic_rate_bps', 2e8)#23.84MBps
        #self.sending_interval_var = 0.2
        self.cwnd = self.params.get('init_cwnd', 5)
        self.awd = self.params.get('awd', 15)
        self.snd_nxt = min(self.cwnd, self.awd)
        self.min_cwnd = self.params.get('min_cwnd', 1)
        self.max_cwnd = self.params.get('max_cwnd', math.inf)
        self.cc_state = self.CC_STATE_SLOW_START
        self.ssthresh = None

        self.chunk_seq = 1
        self.ping_seq = 0
        self.pong_seq = 0
        self.relocated_chunk_seq = -1
    
        #总的统计数据
        self.sent_ping = 0
        self.sent_pong = 0
        self.sent_cwd = 0
        self.received_ping = 0
        self.received_pong = 0
        self.received_cwd = 0
        self.send_times = {}

        #用于PAATP
        self.sent_pkt_rtt = 0   #rtt send ecn
        self.received_pkt_AECN_rtt = 0 #rtt被标记AECN
        self.received_pkt_ce_rtt = 0 #rtt被标记ce
        self.received_pkt_ecn_rtt = 0 #rtt支持ecn
        self.received_pkt_rtt = 0 #rtt

        self.last_pong_received_seq = 0#last_AACK
        self.last_cwd_received_seq = 0#last_GACK
     
        self.cwd_alpha = 0
        self.awd_beta = 0   #初始值应该设置为多少？
        self.aecn_ratio = 0 #初始值应该设置为多少？

        self.factors = 0.125#both f and w
        self.received_pong_from_last_timeout = 0

        self.ping_yet_unpong = {}
        self.out_of_order_cnts = {}

        self.total_chunk_num = int(self.params.get('total_chunk_num', 0))

        self.resend_queue = OrderedDict()

        if self.total_chunk_num <= 1:
            print("Warning: total_chunk_num is less than or equal to 1. No chunks to send.")
        else:
            self.chunk_to_send = OrderedDict()
            for i in range(1, self.total_chunk_num):
                self.chunk_to_send[i] = 1
        self.completed_chunks = {}
        self.completed_chunk_offset = 0

        self.est_rtt = None
        self.dev_rtt = None
        self.rtt_alpha = self.params.get('rtt_alpha', 0.9)
        self.rtt_beta = self.params.get('rtt_beta', 0.9)
        


        self.ecn_enabled = True

        self.last_ssthresh_update_time = -1e3

        self.last_pong_received_time = None

        self.pong_timeout_threshold = self.params.get('timeout_threshold', 100)

        events = []
        if self.total_chunk_num != 0:
            e = Event(self.start_time + self.pong_timeout_threshold, self, "check_timeout")
            events.append(e)
            e = Event(self.start_time, self, "on_ping_sent")
            events.append(e)
        else:
            self.state = self.COMPLETED

        return events

    def restart(self):
        events = []
        if self.state == self.ONLINE:
            return events
        else:
            cur_time = self.get_cur_time()
            self.state = self.ONLINE
            e = Event(cur_time + self.pong_timeout_threshold, self, "check_timeout")
            events.append(e)
            e = Event(cur_time, self, "on_ping_sent")
            events.append(e)
            return events

    def check_completed(self):
        return self.get_completed_chunk_num() >= self.total_chunk_num

    def get_completed_chunk_num(self):
        return 1 + self.completed_chunk_offset + len(self.completed_chunks)

    def check_timeout(self):
        cur_time = self.get_cur_time()
        new_events = []

        self.check_expired(cur_time)
        if self.state != self.ONLINE:
            return new_events

        if len(self.ping_yet_unpong) > 0 and self.received_pong_from_last_timeout == 0:
            print(
                '# Flow',
                self.id,
                'is timeout at',
                cur_time,
            )
            self.cc_state = self.CC_STATE_SLOW_START
            self.ssthresh = max(1., self.cwnd * .5)
            self.update_cwnd(1)
            self.gen_stat()
            self.last_ssthresh_update_time = cur_time
            for i in self.ping_yet_unpong:
                self.resend_queue[i] = 1
            self.ping_yet_unpong.clear()
            self.out_of_order_cnts.clear()

        self.received_pong_from_last_timeout = 0
        e = Event(cur_time + self.pong_timeout_threshold, self, "check_timeout")
        new_events.append(e)
        return new_events

    def send_ping(self, chunk_seq, delay=0.0):
        cur_time = self.get_cur_time()
        pkt = Packet(sent_time=cur_time,
                     priority=self.get_pkt_priority(),
                     pkt_type=self.PING_PKT_TYPE,
                     size_in_bits=self.PING_PKT_SIZE_IN_BITS,
                     flow=self,
                     ecn=Packet.ECT,
                     path=self.path)

        pkt.ping_seq = self.ping_seq
        self.ping_seq += 1
        pkt.chunk_seq = chunk_seq

        self.sent_pkt_rtt += 1

        self.send_times[chunk_seq] = cur_time

        self.sent_ping += 1

        self.ping_yet_unpong[pkt.chunk_seq] = pkt
        self.out_of_order_cnts[pkt.chunk_seq] = 0

        obj = pkt.get_next_hop()
        #delay = random.random() * 0.05 * self.get_sending_interval()
        e = Event(cur_time + delay, obj, 'on_pkt_received', params=dict(pkt=pkt))
        return e, pkt

    def on_ping_sent(self):
        cur_time = self.get_cur_time()
        new_events = []

        self.check_expired(cur_time)
        if self.state != self.ONLINE:
            return new_events

        if self.can_send_ping() and len(self.ping_yet_unpong) <= self.awd:
            if len(self.resend_queue) > 0:      
                chunk_seq, _ = self.resend_queue.popitem(last=False)
                print('$ debug, resend chunk_seq', chunk_seq)
            elif len(self.chunk_to_send) > 0 and self.chunk_seq <= self.snd_nxt:
                chunk_seq = -1
                for chunk_seq in range(self.chunk_seq, self.total_chunk_num):
                    if chunk_seq in self.chunk_to_send:
                        break
                if chunk_seq in self.chunk_to_send:
                    del self.chunk_to_send[chunk_seq]
                else:
                    chunk_seq, _ = self.chunk_to_send.popitem(last=False)

                self.chunk_seq = chunk_seq + 1
               
            elif self.total_chunk_num is None:
                print('undefined impl!')
                raise ValueError
            else:             
                chunk_seq = None

            if chunk_seq is not None:             
                e, _ = self.send_ping(chunk_seq)
                new_events.append(e)

        e = Event(cur_time + self.get_sending_interval(), self, "on_ping_sent")
        new_events.append(e)
        return new_events

    def can_send_ping(self):
        return True

    def on_pkt_received(self, pkt):
        new_events = super().on_pkt_received(pkt)

        if pkt.pkt_type == self.PONG_PKT_TYPE or pkt.pkt_type == self.ACEN_PKT_TYPE:
            lst = self.on_pong_received(pkt)
        elif pkt.pkt_type == self.CWD_PKT_TYPE:
            lst = self.on_cwd_received(pkt)

        return new_events + lst


    def change_ping_to_pong(self, pkt):
        cur_time = self.get_cur_time()
        new_pkt = Packet(sent_time=pkt.sent_time,
                     priority=pkt.priority,
                     pkt_type=self.PONG_PKT_TYPE,
                     size_in_bits=self.PONG_PKT_SIZE_IN_BITS,
                     flow=pkt.flow,
                     ecn=Packet.ECT,
                     path=pkt.flow.reversed_path,
        )

        new_pkt.ping_path = pkt.path
        new_pkt.ping_hop_cnt = pkt.hop_cnt

        new_pkt.ping_seq = pkt.ping_seq
        new_pkt.chunk_seq = pkt.chunk_seq

        new_pkt.hop_cnt = 0
        # deal with ecn flags
        #self.receiver_deal_with_ce(pkt)

        #if self.check_completed(self.received_chunks):
        #    return []
        return new_pkt
    
    def change_ping_to_cwd(self, pkt,Ssum):
    
        cur_time = self.get_cur_time()
        new_pkt = Packet(sent_time=pkt.sent_time,
                priority=pkt.priority,
                pkt_type=self.CWD_PKT_TYPE,
                size_in_bits=self.CWD_PKT_SIZE_IN_BITS,
                flow=pkt.flow,
                ecn=Packet.ECT,
                path=pkt.path,
                
        )
        new_pkt.Ssum = Ssum
        new_pkt.hop_cnt=pkt.hop_cnt
        new_pkt.ping_seq = pkt.ping_seq
        new_pkt.chunk_seq = pkt.chunk_seq
        return new_pkt

 

    def receiver_deal_with_ce(self, pkt):
        pass

    def update_est_rtt(self, sample_rtt):
    
        if self.est_rtt is None:
            self.est_rtt = sample_rtt
        else:
            self.est_rtt = self.est_rtt * self.rtt_alpha + sample_rtt * (1 - self.rtt_alpha)

        sample_dev = sample_rtt - self.est_rtt
        if sample_dev < 0:
            sample_dev *= -1

        if self.dev_rtt is None:
            self.dev_rtt = sample_dev

        self.dev_rtt = self.dev_rtt * self.rtt_beta + sample_dev * (1 - self.rtt_beta)
        self.ack_timeout_interval = self.est_rtt + 4 * self.dev_rtt
        return self.est_rtt

    # TODO: on_pong_sent(self):

    def update_completed_chunk(self, chunk_seq):
        self.completed_chunks[chunk_seq] = 1  # 标记指定的块为完成

        if chunk_seq < self.completed_chunk_offset:
            return  # 如果完成的块在偏移量之前，直接返回

        for i in range(self.completed_chunk_offset, chunk_seq + 1):
            if i in self.completed_chunks:
                del self.completed_chunks[i]  # 删除已完成的块记录
                self.completed_chunk_offset += 1  # 更新偏移量到下一个块
            else:
                return  # 遇到未完成的块，停止处理

        while self.completed_chunk_offset in self.completed_chunks:
            del self.completed_chunks[self.completed_chunk_offset]  # 清理偏移量对应的完成记录
            self.completed_chunk_offset += 1  # 移动偏移量
    def update_awd(self):
        print("szx update awd")
        self.received_pkt_AECN_rtt += 1
        aecn_ratio = self.received_pkt_AECN_rtt / self.sent_pkt_rtt
        self.awd_beta = aecn_ratio * self.factors + self.awd_beta* (1 - self.factors)
        self.awd = (1-self.awd_beta/2) * self.awd

    def update_P(self):
        self.Ssavg = self.Ssum / DeviceNum
        self.Pavg = self.Ssavg - self.last_pong_received_seq
        self.Pl = self.last_cwd_received_seq - self.last_pong_received_seq
        self.P = self.Pl/self.Pavg
        self.Pmax = self.awd/self.Pavg

    def on_pong_received(self, pkt):
        #一些参数变化
        self.received_pong += 1
        self.awd = self.awd + 1
       
        self.received_pkt_rtt += self.PONG_PKT_SIZE_IN_BITS

        if pkt.ecn == pkt.ECT or pkt.ecn == pkt.CE:
            self.received_pkt_ecn_rtt += 1


        if self.state != self.ONLINE:
            return []

        self.received_pong_from_last_timeout += 1
       
        self.update_P()

        cur_time = self.get_cur_time()
        self.last_pong_received_time = cur_time
        sample_rtt = cur_time - self.send_times[pkt.chunk_seq]
        self.update_est_rtt(sample_rtt)
        self.sent_pkt_rtt = (self.sent_pkt_rtt*self.PING_PKT_SIZE_IN_BITS)/sample_rtt
        self.received_pkt_rtt = self.received_pkt_rtt/sample_rtt
        new_chunk_seq = getattr(pkt, 'new_chunk_seq', -1)
        # Newly added
        self.relocated_chunk_seq = new_chunk_seq
        if new_chunk_seq > self.chunk_seq and new_chunk_seq in self.chunk_to_send:
            self.chunk_seq = new_chunk_seq

        self.pong_path_len = len(pkt.path)


        pkt_chunk_seq = pkt.chunk_seq
        self.update_completed_chunk(pkt_chunk_seq)

        if pkt_chunk_seq in self.ping_yet_unpong:
            del self.ping_yet_unpong[pkt_chunk_seq]
            del self.out_of_order_cnts[pkt_chunk_seq]
        elif pkt_chunk_seq in self.resend_queue:
            del self.resend_queue[pkt_chunk_seq]

        self.check_expired(cur_time)
        if self.check_completed():
            self.state = self.COMPLETED
            self.completed_time = cur_time

        if self.state != self.ONLINE:
            return []

        # detect reorder
        to_resend = []
        for i, ipkt in self.ping_yet_unpong.items():
            if ipkt.ping_seq < pkt.ping_seq:
                self.out_of_order_cnts[i] += 1
                if self.out_of_order_cnts[i] >= self.DUPLICATED_PONG_THRESHOLD:
                    to_resend.append(i)
                    print('# debug, out-of-order', self.id, i)
                    del self.out_of_order_cnts[i]
        for chunk_seq in to_resend:
            self.resend_queue[chunk_seq] = 1
            del self.ping_yet_unpong[chunk_seq]

        if len(to_resend) > 0:
            pass


        if self.cc_state == self.CC_STATE_CONGESTION_AVOIDANCE:
            print("szx flow congestion avoidance")
            if pkt.ecn == pkt.CE:
                self.received_pkt_ce_rtt += 1
                if cur_time > self.last_ssthresh_update_time + self.est_rtt:
                    self.last_ssthresh_update_time = cur_time

                    self.cwd_alpha  = (1-self.factors) * self.cwd_alpha 
                    + self.factors * (self.received_pkt_ce_rtt / self.sent_pkt_rtt)
                    self.cwd_alpha = self.cwd_alpha**(1-self.P/self.Pmax)
                    cwnd = max(1, self.cwnd *(1-self.cwd_alpha/2))
                    self.ssthresh = cwnd
                    self.update_cwnd(cwnd)
                    self.gen_stat()
               
            else:
                self.update_cwnd(self.cwnd + 1. / self.cwnd)
                self.gen_stat()

        elif self.cc_state == self.CC_STATE_SLOW_START:
            if pkt.ecn == pkt.CE:
                self.received_pkt_ce_rtt += 1
                # switch to congestion avoidance
                self.cc_state = self.CC_STATE_CONGESTION_AVOIDANCE
                self.last_ssthresh_update_time = cur_time
                
                self.cwd_alpha  = (1-self.factors) * self.cwd_alpha 
                + self.factors * (self.received_pkt_ce_rtt / self.sent_pkt_rtt)
                self.cwd_alpha = self.cwd_alpha**(1-self.P/self.Pmax)
                cwnd = max(1, self.cwnd *(1-self.cwd_alpha/2))
                self.ssthresh = cwnd
                self.update_cwnd(cwnd)
                self.gen_stat()      
            else:
                # stay in slow start
                self.update_cwnd(self.cwnd + 1)
                self.gen_stat()
        else:
            print('# error!')
            raise ValueError


        if(pkt.pkt_type == self.ACEN_PKT_TYPE):
            self.update_awd(pkt.aecn_ratio)
        self.sent_pkt_rtt = 0
        self.received_pkt_AECN_rtt = 0
        self.received_pkt_ce_rtt = 0
        self.received_pkt_ecn_rtt = 0
        self.received_pkt_rtt = 0
        self.last_pong_received_seq = pkt.chunk_seq
        self.snd_nxt = min(self.last_cwd_received_seq+self.cwnd,self.last_pong_received_seq+ self.awd)
        return []
    
    def on_cwd_received(self, pkt):
        self.received_cwd += 1
        self.received_pkt_rtt +=self.CWD_PKT_SIZE_IN_BITS
       

        if(pkt.ecn == pkt.ECT):
            self.received_pkt_ecn_rtt +=1
        if(pkt.ecn == pkt.CE):
            self.received_pkt_ce_rtt +=1
        new_events = []
        cur_time = self.get_cur_time()

        if self.check_expired(cur_time):
            return new_events

        pkt.recv_time = cur_time

        self.cwnd += 1
        self.update_cwnd( self.cwnd + 1)
        self.Ssum = pkt.Ssum
        self.Ssavg = self.Ssum / DeviceNum
        self.snd_nxt = min(self.last_cwd_received_seq+self.cwnd,self.last_pong_received_seq+ self.awd)
        self.last_cwd_received_seq = pkt.chunk_seq
        return new_events
    
    def update_cwnd(self, new_cwnd):
       
        self.cwnd = max(self.min_cwnd, min(self.max_cwnd, new_cwnd))
     

    def on_pkt_lost(self, reason, pkt=None):
        super().on_pkt_lost(reason, pkt)
        # raise ValueError(pkt.pkt_type)
        if pkt.pkt_type == "MDP_PING":
            raise ValueError()
        print('# debug, packet drop', reason, self.id, pkt.ping_seq, pkt.chunk_seq)

    def gen_stat(self):
        cur_time = self.get_cur_time()

        stat = dict(
            ssthresh=self.ssthresh,
            cwnd=self.cwnd,
            rtt=self.est_rtt,
            #rate=self.rate_pps,
            rate_pps = self.cwnd / inst.ideal_rtt,
            ping_seq=self.ping_seq,
            chunk_seq=self.chunk_seq,
            ping_yet_unpong=len(self.ping_yet_unpong),
            sent_ping=self.sent_ping,
            sent_pong=self.sent_pong,
            received_ping=self.received_ping,
            received_pong=self.received_pong,
            awd = self.awd,
            snd_nxt = self.snd_nxt,
            cc_state=self.cc_state,
            sent_pkt_rtt = self.sent_pkt_rtt,
            received_pkt_rtt = self.received_pkt_rtt,
            cwd_alpha = self.cwd_alpha,
            aecn_ratio = self.aecn_ratio,
            rate_bps = self.rate_bps,
            Progress = self.P,
        )
            
        for reason in self.lost_reason:
            key = 'lost_{0}'.format(reason)
            stat[key] = self.lost_reason[reason]
            if hasattr(self, 'old_stat'):
                stat['diff' + key] = stat[key] - self.old_stat.get(key, 0)

        self.pong_path_len = 0
        for i, obj in enumerate(self.path):
            if obj.TYPE == 'LINK':
                stat['q_{0}'.format(i)] = obj.qdisc.get_occupation_in_bits()
        self.stats.append((cur_time, stat))
        self.old_stat = stat



class MUP(PingPongFlow):
    TYPE = 'MUP'
    PING_PKT_TYPE = 'MUP_PING'
    PONG_PKT_TYPE = 'MUP_PONG'
    CWD_PKT_TYPE = 'MUP_CWD'
    ACEN_PKT_TYPE = 'MUP_ACEN'

    PING_PKT_SIZE_IN_BITS = 2400 #300bytes
    PONG_PKT_SIZE_IN_BITS = 2400 #300bytes
    CWD_PKT_SIZE_IN_BITS = 496 #62bytes
    ACEN_PKT_SIZE_IN_BITS = PONG_PKT_SIZE_IN_BITS



class MDPCache(object):
    def __init__(self, max_total_cache_size, max_per_job_cache_size=None):
        self.max_total_cache_size = max_total_cache_size
        self.max_per_job_cache_size = max_per_job_cache_size
        #每个任务最大缓存量，表示每个任务缓存的块数量上限（如果没有指定则默认为 None）。
        self.data = {}
        self.data_flat = OrderedDict()
        self.max_chunk_seq = {}
        self.last_gradient_seq = {}
        self.Ssum = {}
    
    def update_gradient_seq(self, job_id,flow_id,chunk_id):

        if job_id not in self.last_gradient_seq:
            self.last_gradient_seq[job_id] = {}

       
        if flow_id not in self.last_gradient_seq[job_id]:
            self.last_gradient_seq[job_id][flow_id] = chunk_id
        else:
            # 更新最后的梯度序列号
            old_seq = self.last_gradient_seq[job_id][flow_id]
            self.last_gradient_seq[job_id][flow_id] = max(old_seq, chunk_id)

        # 计算当前任务的Ssumj（所有worker的序列号总和）
        self.Ssum[job_id] = sum(self.last_gradient_seq[job_id].values())

        # 输出当前更新信息

        return self.Ssum[job_id]
    

    def move_to_end(self, job_id, chunk_id):
        self.data[job_id].move_to_end(chunk_id)
        self.data_flat.move_to_end((job_id, chunk_id))

    def has_cache(self, job_id, chunk_id):

        return (job_id, chunk_id) in self.data_flat
        if job_id not in self.data:
            return False
        else:
            return chunk_id in self.data[job_id]

    def update_cache(self, job_id, chunk_id, cur_time):

        self.data_flat[(job_id, chunk_id)] = cur_time
        self.data.setdefault(job_id, OrderedDict())[chunk_id] = cur_time
        self.max_chunk_seq[job_id] = max(chunk_id, self.max_chunk_seq.get(job_id, -1))

        if len(self.data_flat) > self.max_total_cache_size:
            ((job_id, chunk_id), t) = self.data_flat.popitem(last=False)
            del self.data[job_id][chunk_id]
            if chunk_id == self.max_chunk_seq[job_id]:
                # todo: fix bugs here
                if len(self.data[job_id]) == 0:
                    self.max_chunk_seq[job_id] = -1
                else:
                    self.max_chunk_seq[job_id] = max(self.data[job_id].keys())

    def get_relocation_seq(self, job_id, chunk_id):
        return self.max_chunk_seq[job_id]

    def update_cache_and_get_new_chunk_seq(self, job_id, chunk_id, cur_time, relocation_enabled=False):
        pass
    #szx修改后    
    def __len__(self):

        return len(self.data_flat)
    #szx修改前
    def __delitem__(self, key):
        job_id, chunk_id = key
        # 从 data_flat 中删除项
        del self.data_flat[key]
        # 从 data 中删除项
        del self.data[job_id][chunk_id]
        # 如果该项是该任务的最大序列，更新最大序列
        if chunk_id == self.max_chunk_seq[job_id]:
            if len(self.data[job_id]) == 0:
                self.max_chunk_seq[job_id] = -1
            else:
                self.max_chunk_seq[job_id] = max(self.data[job_id].keys())

class EdgeBox(Middlebox):
    STOPPED = 0
    RUNNING = 1

    def __init__(
            self,
            mdp_cache_algorithm=None,
            mup_cache_algorithm=None,
            max_mdp_cache_size=1e6,  
            max_mup_cache_size=1e6,
            mup_timeout=10,
            enabled=True,
            jobs_config={},
            relocation_enabled=False):
        super().__init__()
        self.nfs = {}
        self.nfs[MUP.TYPE] = self.process_mup

        self.mdp_cache = MDPCache(max_mdp_cache_size)  #OrderedDict()
        #mdp_cache 和 mup_cache 使用了 MDPCache 来管理缓存。
        self.mdp_perf_metrics = dict(
            total={},
            detail={},
        )
        self.mup_cache = MDPCache(max_mup_cache_size) 
        self.mup_cache_meta = {}
        self.mup_perf_metrics = dict(
            total=0,
            detail=dict(timeout=0, overflow=0, completed=0),
        )

        self.mdp_cache_algorithm = mdp_cache_algorithm
        self.mup_cache_algorithm = mup_cache_algorithm
        self.max_mdp_cache_size = max_mdp_cache_size
        self.max_mup_cache_size = max_mup_cache_size

        self.mup_timeout = mup_timeout

        self.flows_of_each_job = {}

        self.jobs_config = jobs_config

        self.enabled = enabled
        self.perf_metrics = dict(sent={}, received={})

        self.ebmup_flows = {}

        self.remove_timeout_mupchunk_running = False
        self.relocation_enabled = relocation_enabled

    def stop(self):
        self.state = self.STOPPED

    def register_ebmup(self, job_id, f):

        self.ebmup_flows[job_id] = f

    def process(self, pkt):
        
        self.perf_metrics['received'][pkt.pkt_type] = self.perf_metrics['received'].get(pkt.pkt_type, 0) + 1
        if self.enabled:
            pkts = self.nfs[pkt.flow.TYPE](pkt)
        for pkt in pkts:
            self.perf_metrics['sent'][pkt.pkt_type] = self.perf_metrics['sent'].get(pkt.pkt_type, 0) + 1
        return pkts

    def change_ping_to_pong(self, pkt):
       

        return pkt.flow.change_ping_to_pong(pkt)
    
    def change_ping_to_cwd(self, pkt,Ssum):
        return pkt.flow.change_ping_to_cwd(pkt,Ssum)


    def register_env(self, env):
        self.env = env  # 注册 net 实例
    def enable_remove_timeout_mupchunk(self):
        if self.remove_timeout_mupchunk_running:
            return
        else:
            events = self.remove_timeout_mupchunk()
            self.env.add_events(events)
    def remove_timeout_mupchunk(self):
        # 获取当前时间
        cur_time = self.get_cur_time()
        to_remove = []
        t_threshold = cur_time - self.mup_timeout

        # 遍历 `data_flat` 查找超时条目
        for chunk_key, cache_entry in list(self.mup_cache.data_flat.items()):  # 使用 list() 以避免修改字典时出错
            if cache_entry < t_threshold:
                to_remove.append(chunk_key)
            else:
                break

        # 移除超时条目并加入发送队列
        for k in to_remove:
            del self.mup_cache.data_flat[k]
            del self.mup_cache.data[k[0]][k[1]]  # 从 `data` 中删除对应条目
            self.add_aggregated_to_send_queue(chunk_key=k, reason='timeout')

        # 检查缓存是否为空
        new_events = []
        if len(self.mup_cache.data_flat) == 0:
            self.remove_timeout_mupchunk_running = False
        else:
            # 重新安排下次事件
            e = Event(cur_time + self.mup_timeout, self, "remove_timeout_mupchunk")
            new_events.append(e)

        return new_events

    def process_mup(self, pkt):
        #return pkt
        #处理 MUP 类型数据包。与 MDP 类似，根据数据包类型进行处理
        job_id = pkt.flow.job_id
        flow_id = pkt.flow.id

        chunk_key = (job_id, pkt.chunk_seq)

        cur_time = self.get_cur_time()
        pkts = []
        if pkt.pkt_type == MUP.PING_PKT_TYPE:
            #PING: 更新缓存，如果 MUP 数据包满足条件，则将其标记为已完成或溢出
            if flow_id not in self.mup_cache_meta.get(chunk_key, []):
                # ignore duplicated packet
                self.mup_cache_meta.setdefault(chunk_key, set()).add(flow_id)
                Ssum = self.mup_cache.update_gradient_seq(job_id,flow_id, pkt.chunk_seq)
                pkt1 = self.change_ping_to_cwd(pkt,Ssum)

                pkts.append(pkt1)

                if not self.mup_cache.has_cache(job_id, pkt.chunk_seq):
                    self.mup_cache.update_cache(job_id, pkt.chunk_seq, cur_time)
                    self.enable_remove_timeout_mupchunk()
                else:
                    pass

                if len(self.mup_cache_meta[chunk_key]) >= self.jobs_config[job_id]['flownum']:
                    # remove completed
                    del self.mup_cache[chunk_key]
                    # self.add_aggregated_to_send_queue(chunk_key=chunk_key, reason='completed')
                    pkt2 = self.change_ping_to_pong(pkt)
                    pkts.append(pkt2)
                #TODO
                # elif len(self.mup_cache) > self.max_mup_cache_size:
                    # # remove oldest in case of overflow
                    # k, _ = self.mup_cache.popitem(last=False)
                    
                    # # self.add_aggregated_to_send_queue(chunk_key=k, reason='overflow')
                    # pkt2 = copy.deepcopy(self.change_ping_to_pong(pkt))
                    # pkts.append(pkt2)
        elif pkt.pkt_type == MUP.PONG_PKT_TYPE:
            line = inst.Eta*inst.M_and_BDP
          

            if len(self.mup_cache.data_flat) >line :
               print("SZX acen happened")
               pkt.pkt_type = MUP.ACEN_PKT_TYPE
           
            flows_for_job_i = [f for f in inst.flows if f.job_id == pkt.flow.job_id]
            for flow in flows_for_job_i:
                    new_pkt = Packet(sent_time=cur_time,
                                priority=pkt.priority,
                                pkt_type=pkt.pkt_type,
                                size_in_bits=MUP.ACEN_PKT_SIZE_IN_BITS,
                                flow=flow,
                                ecn=pkt.ecn,
                                path=flow.reversed_path,
                    )
                    new_pkt.ping_path = pkt.path
                    new_pkt.ping_hop_cnt = pkt.hop_cnt
                    new_pkt.ping_seq = pkt.ping_seq
                    new_pkt.chunk_seq = pkt.chunk_seq
                    new_pkt.hop_cnt = pkt.hop_cnt
                    pkts.append(new_pkt)
        else:
            raise ValueError
        return pkts

def reset_obj_ids():
 
    Link._next_id = 1
    Flow._next_id = 1
    Middlebox._next_id = 1

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
    plt.tight_layout()
    plt.show()


def mkAXS(key=0):
    ax = _fig()
    axs[key] = ax
    return ax

class Demo(object):
    max_mup_cache_size = 40000
    def __init__(self,
                 DeviceNum=1,
                 JobNum=1,
                 bw_bps=80e6,
                 lat=1e-2,##链路延迟，单位为秒。
                 loss=0,##: 数据包丢失率。
                 FCLS=MUP,
                 MODEL_SIZE_IN_CHUNK=50e3,##模型大小，用于设置总数据包数
                 max_mdp_cache_size=1e4,#缓存大小。
                 max_mup_cache_size=max_mup_cache_size,##缓存大小。
                 enabled_box=True,#是否启用 EdgeBox（边缘设备）。
                 relocation_enabled=False,#
                 queue_lambda=1.,
                 start_times=None):

        self.link_bw_bps = bw_bps
        self.loss_rate = loss
        self.queue_lambda = queue_lambda

        ideal_rtt = lat * 2
        self.ideal_rtt = ideal_rtt
        self.bw_bps = bw_bps
        bdp_in_bits = int(bw_bps * ideal_rtt)
        qsize_in_bits = bdp_in_bits * 4

        max_cwnd = qsize_in_bits * 2 / 8 / BYTES_PER_PACKET
        
        self.CA_and_N = ((bw_bps*ideal_rtt)/MUP.PING_PKT_SIZE_IN_BITS)*JobNum+JobNum
        self.M_and_BDP = max_mup_cache_size + (bw_bps*ideal_rtt*DeviceNum)/MUP.PING_PKT_SIZE_IN_BITS
        self.Eta = 1-self.CA_and_N/self.M_and_BDP
        line = self.Eta*(max_mup_cache_size+(self.bw_bps*self.ideal_rtt*DeviceNum)/MUP.PING_PKT_SIZE_IN_BITS)
        print("SZX CA_and_N:",self.CA_and_N)
        print("SZX M_and_BDP:",self.M_and_BDP)
        print("self.Eta:",self.Eta)
        print("SZX ACEN line:",line)
        print("max_cwnd:",max_cwnd)
      

        weights = {1: 8, 2: 4, 3: 2, 4: 1}
        mq_sizes = {i: qsize_in_bits for i in weights}
        RedConfigs = {i: dict(MinTh_r=0.3, MaxTh_r=0.7, wq=0.002, UseEcn=True, UseHardDrop=False) for i in mq_sizes}

        qdisc = DWRR(mq_sizes, RedConfigs, weights=weights)
        #qdisc：创建一个字典，键为 mq_sizes 中的每个值，值是一个包含 RED（Random Early Detection）算法配置的字典。
        mup_jobs_config = {}
        for i in range(JobNum):
            mup_jobs_config[i] = dict(flownum=DeviceNum)

        self.boxes = [
            EdgeBox(
                jobs_config=mup_jobs_config,
                max_mdp_cache_size=max_mdp_cache_size,  
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
                        self.links[2*j+1],#1，3
                    ],
                    rpath=[
                        self.links[-1],#5
                        self.links[-2],#4
                        box,
                        self.links[2*j+1],#1，3
                    ],
                    nic_rate_pps=2 * bw_bps,
                    job_id=i,
                    start_time=start_times[j],  #0, 
                    #expired_time=20,
                    total_chunk_num=MODEL_SIZE_IN_CHUNK,
                    max_cwnd=max_cwnd,
                )
                self.flows.append(f)


            box.register_ebmup(job_id=i, f=f)#调用 box 的 register_ebmup 方法，注册此流到某个调度或管理模块。

        self.net = Environment(self.flows, self.links, self.boxes)

    def runsim(self):
        self.net.run()


    def plot(self):
        spec_set = set()
        ax_dict = {}
        output = {}
        plot_data = {}
        
        # 收集每个 flow 的数据
        for f in self.flows:
            print('flow', f.id, 'sent_ping', f.sent_ping, 'received_ping', f.received_ping, 'sent_pong', f.sent_pong, 'received_pong', f.received_pong)
            print('drop stat', f.lost_reason)
            print('expired time', f.expired_time)

            for t, stat in f.stats:  # f.stats是一个元组构成的数组，t为时间，stat为流的状态（一个字典）
                for k, v in stat.items():  # 遍历状态字典中的每个键值对
                    if k not in plot_data:
                        plot_data[k] = {}
                    if f.id not in plot_data[k]:
                        plot_data[k][f.id] = []
                    if isinstance(v, list):
                        plot_data[k][f.id].extend(v)
                        spec_set.add(k)
                    else:  # 只有数值型的属性需要添加时间信息
                        plot_data[k][f.id].append((t, v))

        # 创建一个单独的图，或者根据需要的子图数量动态分配
        for k in plot_data:
            # 为每个 flow 创建独立的子图
            fig, ax = plt.subplots(len(plot_data[k]), 1, figsize=(10, 6 * len(plot_data[k])))  # 创建多行子图，每个flow一个子图
            fig.suptitle(k, fontsize=16)  # 设置每个图的标题为统计项 k
            if len(plot_data[k]) == 1:  # 如果只有一个flow，ax 会是一个单一的坐标轴对象
                ax = [ax]
            
            # 对每个 flow 分别绘图
            for idx, (fid, data) in enumerate(plot_data[k].items()):
                x, y = [], []  # x为时间，y为数值
                for xx, yy in data:
                    x.append(xx)
                    y.append(yy)
                ax[idx].errorbar(x=x, y=y, label='flow {0}'.format(fid))
                ax[idx].set_xlabel('时间 (秒)')
                ax[idx].set_ylabel(k)
                ax[idx].legend()

        plt.tight_layout()
        plt.show()


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

if __name__ == '__main__':

    for FCLS in (MUP, ):  #MUP
        gradient_num = args.num
        JobNum = args.jobnum
        #a = 0.1
        max_mdp_cache_size = 1e4
        max_mup_cache_size=40000
        DeviceNum = 1
        for relocation_enabled in (True, False)[:1]:
            inst = Demo(
                DeviceNum=DeviceNum,
                JobNum=JobNum,
                FCLS=FCLS,
                MODEL_SIZE_IN_CHUNK=gradient_num + 1,#加1是因为代码原因
                max_mdp_cache_size=max_mdp_cache_size,
                max_mup_cache_size=max_mup_cache_size,
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
