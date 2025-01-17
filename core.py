# Copyright 2019 Nathan Jay and Noga Rotman
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and

# limitations under the License.

import time

import heapq
import random

from collections import deque
from copy import deepcopy


MAX_RATE_BPS = 1e10
MIN_RATE_BPS = 1


USE_LATENCY_NOISE = False
MAX_LATENCY_NOISE = 1.1

BYTES_PER_PACKET = 1500


def calc_mean(lst):  # 计算平均值
    if len(lst) == 0:
        return 0.
    else:
        return sum(lst) * 1.0 / len(lst)


class Event(object):##定义了事件，事件可以被执行 (exec) 来触发特定对象的方法
    def __init__(self, t, obj, name, params={}):
        self.t = t
        self.obj = obj
        self.name = name
        self.params = params
    
    def __lt__(self, other):
        return (self.t, self.obj.TYPE, self.name, self.obj) < (other.t, other.obj.TYPE,  other.name, other.obj)

    def exec(self):
        #print('# event debug', self.t, self.obj, self.name, self.params)
        return getattr(self.obj, self.name)(**self.params)        


class EnvObject(object):##所有模拟环境中的对象的基类，提供了状态和时间相关的方法
    UNDEFINED = 0
    STOPPED = -1

    def __init__(self):
        self.state = self.UNDEFINED
        self.env = None

    def register_env(self, env):
        self.env = env

    def get_cur_time(self):
        return self.env.get_cur_time()

    def start(self):
        return []

    def stop(self):
        self.state = self.STOPPED
        return []

    def info(self, msg):
        t = self.get_cur_time()
        print('Time', t, msg)

    def __lt__(self, other):
        return self.id < other.id


class FIFO(object):##实现了简单的先进先出队列，支持包的入队和出队操作。
    def __init__(self):
        self.q = deque()
        self.total_pkt_cnt = 0
        self.total_pkt_size_in_bits = 0

    def clear(self):
        self.q.clear()
        self.total_pkt_cnt = 0
        self.total_pkt_size_in_bits = 0

    def enq(self, pkt):
        self.q.append(pkt)
        self.total_pkt_cnt += 1
        self.total_pkt_size_in_bits += pkt.size_in_bits
    
    def get_next_pkt(self):
        if self.total_pkt_cnt == 0:
            return None
        else:
            return self.q[0]

    def deq(self):
        if self.total_pkt_cnt == 0:
            return None
        else:
            pkt = self.q.popleft()
            self.total_pkt_cnt -= 1
            self.total_pkt_size_in_bits -= pkt.size_in_bits
            return pkt

    def __len__(self):
        return self.total_pkt_cnt
    
    def get_pkt_num(self):
        return self.total_pkt_cnt

    def get_occupation_in_bits(self):
        return self.total_pkt_size_in_bits


class QdiscBase(EnvObject):#queueing discipline，排队规则
    def __init__(self):
        super().__init__()
    
    def is_empty(self):
        return True

    def enq(self, pkt):
        pass

    def deq(self):
        return None
    
    def get_occupation(self):
        pass


class Qdisc(QdiscBase):##定义了网络中的排队规则，支持多种不同的调度算法：
    #https://www.geeksforgeeks.org/random-early-detection-red-queue-discipline/
    def __init__(self, mq_sizes, RedConfigs):
        super().__init__()
        #red_threshold_factors=None, ecn_threshold_factors=None):
        self.priorities = sorted(mq_sizes)
        self.mq = {i: FIFO() for i in self.priorities} # to the dict of priority queues
        self.mq_sizes = mq_sizes
        self.RedConfigs = RedConfigs
        #MinTh_r, MaxTh_r, UseEcn, UseHardDrop
        self.p_index = 0
        for p, q in self.mq.items():
            q.count = 0
            q.avg_qlenr = 0
            q.size_bits = self.mq_sizes[p]
            q.empty_start_time = 0
        
        self.avg_pkt_transmit_time = 1e-9
        
    def enq(self, pkt):
        p = pkt.priority
        q = self.mq[p]
        RedConfig = self.RedConfigs[p]
        wq = RedConfig.get('wq', 0.002)

        if q.total_pkt_size_in_bits < 1:
            cur_time = self.get_cur_time()
            if q.empty_start_time is None:
                q.empty_start_time = cur_time
            m = (cur_time - q.empty_start_time) / self.avg_pkt_transmit_time
            q.avg_qlenr = (1 - wq) ** m * q.avg_qlenr
        else:
            q.avg_qlenr = (1 - wq) * q.avg_qlenr + wq *  q.total_pkt_size_in_bits / q.size_bits
            q.empty_start_time = None

        if  q.avg_qlenr >= RedConfig['MaxTh_r']:
            pkt.drop(pkt.DROP_CAUSED_BY_CONGESTION)
            q.count = 0
            return False
        elif q.avg_qlenr > self.RedConfigs[p]['MinTh_r']:
            if RedConfig.get('UseEcn', False) and pkt.ecn == pkt.ECT:
                pkt.ecn = pkt.CE
            
            pd = RedConfig.get('maxp', 0.5) * (q.avg_qlenr - RedConfig['MinTh_r']) / (RedConfig['MaxTh_r'] - RedConfig['MinTh_r'])
            x = 1 - q.count * pd
            if x <= 0:
                pa = 1
            else:
                pa = min(1, pd / x)

            if RedConfig.get('UseHardDrop', False) or random.random() < pa:
                    pkt.drop(pkt.DROP_CAUSED_BY_CONGESTION)
                    q.count = 0
                    return False
        q.enq(pkt)
        q.count += 1
        return True
    
    def deq(self):
        return self.get_next_pkt(with_deq=True)

    def get_next_pkt(self, with_deq=False):
        #self.p_index = self.p_index % len(self.priorities)
        old_p_index = self.p_index
        while True:
            p = self.priorities[self.p_index]
            if len(self.mq[p]) > 0:
                if with_deq:
                    self.p_index =  (self.p_index + 1) % len(self.priorities)
                    return self.mq[p].deq()
                else:
                    return self.mq[p].get_next_pkt()
            else:
                self.p_index =  (self.p_index + 1) % len(self.priorities)
            
            if self.p_index == old_p_index:
                return None        

    def is_empty(self):
        for i in self.priorities:
            if len(self.mq[i]) > 0:
                return False
        return True
    
    def get_occupation_in_bits(self):#获取mq中包的总个数
        s = 0
        for q in self.mq.values():
            s += q.get_occupation_in_bits()
        return s

    def get_pkt_num(self):
        n = 0
        for q in self.mq.values():
            n += len(q)
        return n

    def copy(self):
        return deepcopy(self)


class SPQ(Qdisc):##调度算法实现严格优先级调度。
    "Strict Priority Queue" 
    def get_next_pkt(self, with_deq=False):
        for i in self.priorities:
            if len(self.mq[i]) > 0:
                if with_deq:
                    return self.mq[i].deq()
                else:
                    return self.mq[i].get_next_pkt()
        return None


class DWRR(Qdisc):#Deficit Weighted Round Robin，加权差额循环调度。实现严格优先级调度。
    """
    https://en.wikipedia.org/wiki/Deficit_round_robin
    """
    Quantum = 1200 * 8
    def __init__(self, mq_sizes, RedConfigs, weights=None):
        super().__init__(mq_sizes, RedConfigs)
        if weights is None:
            self.weights = {i: 1 for i in self.priorities} #相等权重
        else:
            self.weights = weights    
        #max_weight = max(self.weights.values())
        min_weight = min(self.weights.values())
        self.DC = {i: 0 for i in self.priorities} #DeficitCounter
        self.Q = {i: self.Quantum * w / min_weight for i, w in self.weights.items()} #Quantum

    def deq(self):
        while 1:
            is_empty = True
            for i in self.priorities:
                if len(self.mq[i]) == 0:
                    self.DC[i] = 0
                    continue
                is_empty = False
                # all packets are with the equal size of 1
                pkt = self.mq[i].get_next_pkt()
                if self.DC[i] >= pkt.size_in_bits:
                    self.DC[i] -= pkt.size_in_bits
                    return self.mq[i].deq()
            assert not is_empty
            for i in self.priorities:
                self.DC[i] += self.Q[i]
        return None

    def get_next_pkt(self):
        while 1:
            is_empty = True
            for i in self.priorities:
                if len(self.mq[i]) == 0:
                    self.DC[i] = 0
                    continue
                is_empty = False
                # all packets are with the equal size of 1
                pkt = self.mq[i].get_next_pkt()
                if self.DC[i] >= pkt.size_in_bits:
                    return pkt
            assert not is_empty
            for i in self.priorities:
                self.DC[i] += self.Q[i]
        return None


class WFQ(DWRR):##加权公平队列（继承自DWRR，但未完全实现）。
    """
    https://en.wikipedia.org/wiki/Weighted_fair_queueing
    """
    def __init__(self, weights, mq_sizes, ecn_threshold_fractors=None):
        self.weights = weights
        super().__init__(mq_sizes, ecn_threshold_fractors)
        # TODO:  


class Link(EnvObject):##表示网络中的链路，负责包的发送、接收和带宽计算。
    TYPE = "LINK"
    _next_id = 1
    def _get_next_id():
        result = Link._next_id
        Link._next_id += 1
        return result

    def __init__(self, bandwidth, delay, qdisc, loss_rate=0, **params):
        super().__init__()
        self.id = Link._get_next_id()

        self.bw_bps = float(bandwidth)

        self.qdisc = qdisc
        self.qdisc.avg_pkt_transmit_time = BYTES_PER_PACKET * 8 / self.bw_bps

        self.delay = delay
        self.lr = loss_rate
        self.unscheduled = True
        self.params = params 
    
    def register_env(self, env):
        self.qdisc.register_env(env)
        return super().register_env(env)

    def __lt__(self, other):
        return self.id < other.id

    def get_next_sending_interval(self, pkt_size_in_bits):
        return pkt_size_in_bits / self.bw_bps
        
    def config_red(self, RedConfigs):
        pass
        #self.qdisc.config(RedConfigs=RedConfigs)

    def update_bw(self, new_bw_bps):
        self.bw_bps = new_bw_bps
        return []

    def pkt_enq(self, pkt):
        #current_time = self.get_cur_time()
        if random.random() < self.lr:
            pkt.drop(pkt.DROP_CAUSED_BY_LINK_ERROR)
            return False
        else:
            return self.qdisc.enq(pkt)
        
    def pkt_deq(self):
        #current_time = self.get_cur_time()
        return self.qdisc.deq()

    def on_pkt_sent(self):
        new_events = []

        cur_time = self.get_cur_time()
        pkt = self.pkt_deq()
        if pkt is not None:            
            obj = pkt.get_next_hop()
            e = Event(
                cur_time + self.delay, 
                obj, 'on_pkt_received', 
                params=dict(pkt=pkt))

            new_events.append(e)
        else:
            assert self.qdisc.is_empty()
            
        if not self.qdisc.is_empty():#队列不空
            next_pkt = self.qdisc.get_next_pkt()
            t = self.get_next_sending_interval(next_pkt.size_in_bits)
            e = Event(cur_time + t, self, 'on_pkt_sent')
            new_events.append(e)
        else:
            self.unscheduled = True
        return new_events

    def on_pkt_received(self, pkt):
        print("szx on_pkt_received link")
        pkt.hop()

        cur_time = self.get_cur_time()
        flag = self.pkt_enq(pkt)  # 链路收到包，包进入队列
        new_events = []
        if self.unscheduled:#链路状态
            if not self.qdisc.is_empty():
                self.unscheduled = False
                next_pkt = self.qdisc.get_next_pkt()
                t = self.get_next_sending_interval(next_pkt.size_in_bits)
                e = Event(cur_time + t, self, 'on_pkt_sent')
                new_events.append(e)
        return new_events

        
class Environment(object):##定义模拟环境，管理事件队列，并调度事件的执行。
    def __init__(self, flows, links, boxes=[]):
        self.events = []
        self.cur_time = 0.0
        self.flows = flows
        self.links = links
        self.boxes = boxes

        self.init_links()
        self.init_flows()
        self.init_boxes()

    def init_flows(self):
        for f in self.flows:
            f.register_env(self)
            init_events = f.start()
            for e in init_events:
                heapq.heappush(self.events, e)  # 将事件压入堆栈中

    def add_events(self, events):
        for e in events:
            heapq.heappush(self.events, e)

    def stop_flows(self):
        pass
    
    def init_links(self):
        for l in self.links:
            l.register_env(self)
    
    
    def stop_links(self):
        pass

    def init_boxes(self):
        for b in self.boxes:
            b.register_env(self)
    
    def stop_boxes(self):
        for b in self.boxes:
            b.stop()

    def get_cur_time(self):
        return self.cur_time

    def run(self, end_time=None):

        while end_time is None or self.cur_time < end_time:
            if len(self.events) == 0:
                break
            e = heapq.heappop(self.events)

            # 打印当前事件队列的状态
            # print(f"Current length of events queue: {len(self.events)}")
            # print(f"Inserting event: {e}, Event time: {e.t}, Event name: {e.name}")
            
            assert e.t >= self.cur_time
            self.cur_time = e.t

            # # 打印事件插入前后的队列状态
            # print(f"Before inserting event, events queue length: {len(self.events)}")
            # for event in self.events:
            #     print(f"Event time: {event.t}, Event name: {event.name}")

            new_events = e.exec()
            for e in new_events:
                heapq.heappush(self.events, e)

            # print(f"After inserting event, events queue length: {len(self.events)}")
            # for event in self.events:
            #     print(f"Event time: {event.t}, Event name: {event.name}")

        
        self.stop_boxes()

    def set_delayed_link_bw_update(self, delay, link, beta):
        pass


class Middlebox(EnvObject):##提供了一个Middlebox类，可以自定义处理包的逻辑。
    TYPE = "Middlebox"

    _next_id = 1
    def _get_next_id():
        result = Middlebox._next_id
        Middlebox._next_id += 1
        return result

    def __init__(self):
        super().__init__()
        self.id = Middlebox._get_next_id()

    def on_pkt_received(self, pkt):
        print("szx on_pkt_received middlebox")
        pkt.hop()
        pkts = self.process(pkt)

        cur_time = self.get_cur_time()
        events = []

        for pkt in pkts:
            obj = pkt.get_next_hop()
            e = Event(cur_time, obj, 'on_pkt_received', params=dict(pkt=pkt))
            events.append(e)
        
        return events

    def process(self, pkt):
        return pkt

    def stop(self):
        pass


class Packet(object):##表示网络中的数据包，包含优先级、大小和路径信息。
    DROP_CAUSED_BY_LINK_ERROR = 1
    DROP_CAUSED_BY_CONGESTION = 2
    
    NOT_ECT = 0
    ECT = 1
    CE = 3
    def __init__(self, sent_time, priority, pkt_type=None, size_in_bits=0, flow=None, path=[], ecn=NOT_ECT, ece=None):
        self.sent_time = sent_time
        self.recv_time = None
        self.ack_time = None

        self.pkt_type = pkt_type
        self.size_in_bits = size_in_bits

        self.ecn = ecn
        self.ece = ece
        self.ace = False

        self.dropped = None
        self.enq_time = None
        self.deq_time = None

        self.priority = priority  # 优先级来自流的优先级
        self.flow = flow
        self.path = path

        self.hop_cnt = 0

        self.Ssum = 0

    def hop(self):
        self.hop_cnt += 1

    def get_next_hop(self):
        if self.hop_cnt >= len(self.path):
            print('error', self.hop_cnt, self.path)
            raise ValueError
            return None
        else:
            return self.path[self.hop_cnt]

    def get_pkt_size(self):
        return self.size_in_bits
    
    def drop(self, reason):
        self.dropped = True
        #print('debug drop packet for flow', self.flow.id, reason)
        self.flow.on_pkt_lost(reason, self)

    def __lt__(self, other):
        if self.flow.id == other.flow.id:  # 流相同则比较时间
            return self.sent_time < other.sent_time
        elif self.priority != other.priority:  # 比较优先级
            return self.priority < other.priority
        elif self.enq_time is not None and other.enq_time is not None:  # 比较入队时间
            return self.enq_time < other.enq_time
            #return random.choice([True, False])
        else:
            return self.sent_time < other.sent_time
    
    def __gt__(self, other):
        return not self.__lt__(other)
    

class Flow(EnvObject):##表示网络中的数据流。
    UNSTARTED=1
    ONLINE=2
    COMPLETED=3
    EXPIRED=4

    TYPE = 'Flow'
    _next_id = 1
    def _get_next_id():
        result = Flow._next_id
        Flow._next_id += 1
        return result
    
    def __lt__(self, other):
        return True

    def __gt__(self, other):
        return True

    def __le__(self, other):
        return True

    def __ge__(self, other):
        return True

    def __init__(self, path, rpath, 
                 start_time=0, expired_time=None,
                 init_rate_bps=None, nic_rate_bps=None,
                 priority=1, **params):
        super().__init__()
        self.id = Flow._get_next_id()
        self.priority = priority

        self.start_time = start_time
        self.expired_time = expired_time
        if self.expired_time is not None:
            assert self.expired_time > self.start_time

        self.init_rate_bps = init_rate_bps
        self.nic_rate_bps = nic_rate_bps
        self.rate_bps = init_rate_bps
        if self.rate_bps is None:
            self.rate_bps = self.nic_rate_bps

        self.pacing_factor = 1.

        self.min_rate_bps = params.get('min_rate_bps', MIN_RATE_BPS)
        self.max_rate_bps = params.get('max_rate_bps', MAX_RATE_BPS)
        self.min_rate_delta = params.get('min_rate_delta', -1)
        self.max_rate_delta = params.get('max_rate_delta',  1)
        #self.check_completed = params.get('check_completed', lambda v: False)
        
        self.ecn_enabled = params.get('ecn_enabled', None)

        self.state = self.UNSTARTED

        self.stats = []
        self.lost_reason = {}
        self.total_lost = 0
        
        self.path = list(path)
        self.reversed_path = list(rpath)
        self.path.append(self)
        self.reversed_path.append(self)#PAATP中指的是ACKpath


        self.default_pkt_size_in_bits = params.get('pkt_size_in_bits', 8e3)

        self.params = params
        
    def check_expired(self, cur_time=None):
        if self.expired_time is None:
            return False

        if self.state == self.ONLINE:
            if cur_time is None:
                cur_time = self.get_cur_time()
            if cur_time > self.expired_time:
                print('# Flow', self.id, 'expired at', cur_time)
                self.state = self.EXPIRED
        return self.state == self.EXPIRED

    def get_pkt_priority(self):
        return self.priority

    def on_pkt_received(self, pkt):
        # print("szx on_pkt_received flow")
        pkt.hop()
        return []

    def get_sending_interval(self):
        return self.pacing_factor * self.default_pkt_size_in_bits / self.rate_bps
    
    def get_waiting_interval(self, r=0.1):
        return r * self.get_sending_interval()

    def start(self):
        self.state = self.ONLINE
        return []

    def gen_stat(self):
        pass
    
    def update_rate(self, new_rate_bps):
        #self.new_rate_bps = 200
        self.set_rate(new_rate_bps)
        return []

    def on_pkt_lost(self, reason, pkt=None):
        self.total_lost += 1
        self.lost_reason[reason] = 1 + self.lost_reason.get(reason, 0)
    
    def apply_rate_delta(self, delta):
        #delta *= config.DELTA_SCALE
        #print("Applying delta %f" % delta)
        if delta >= 0.0:
            self.set_rate(self.rate_bps * (1.0 + delta))
        else:
            self.set_rate(self.rate_bps / (1.0 - delta))

    def set_rate(self, new_rate_bps):
        max_next_rate = min(self.max_rate_bps, self.rate_bps * (1. + self.max_rate_delta))
        min_next_rate = max(self.min_rate_bps, self.rate_bps / (1. - self.min_rate_delta))
        if new_rate_bps > max_next_rate:
            new_rate_bps = max_next_rate
        elif new_rate_bps < min_next_rate:
            new_rate_bps = min_next_rate
        self.rate_bps = new_rate_bps
        #print("Attempt to set new rate to %f (min %f, max %f)" % (new_rate, MIN_RATE, MAX_RATE))

