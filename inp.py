import math

from core import *

from collections import OrderedDict

# http://intronetworks.cs.luc.edu/current/html/reno.html
# Computer Networking A Top-Down Approach Seventh Edition


class PingPongFlow(Flow):
    #PING：用于探测网络的请求数据包。
    #PONG：对 PING 数据包的响应。
    CC_STATE_SLOW_START = 1
    CC_STATE_CONGESTION_AVOIDANCE = 2
    CC_STATE_FAST_RECOVERY = 3

    PING_PKT_TYPE = 'PING'
    PONG_PKT_TYPE = 'PONG'
    CWD_PKT_TYPE = 'CWD'

    PING_PKT_SIZE_IN_BITS = 2
    PONG_PKT_SIZE_IN_BITS = int(1.2 * 8e3)
    CWD_PKT_SIZE_IN_BITS = 2

    DUPLICATED_PONG_THRESHOLD = 3

    def start(self):
        super().start()
        self.job_id = self.params.get('job_id', 0)

        self.rate_bps = self.params.get('nic_rate_bps', 2e8)
        #self.sending_interval_var = 0.2
        self.cwnd = self.params.get('init_cwnd', 1)
        self.min_cwnd = self.params.get('min_cwnd', 1)
        self.max_cwnd = self.params.get('max_cwnd', math.inf)
        self.cc_state = self.CC_STATE_SLOW_START
        self.ssthresh = None

        self.chunk_seq = 0
        self.ping_seq = 0
        self.pong_seq = 0
        self.relocated_chunk_seq = -1

        self.sent_ping = 0
        self.sent_pong = 0
        self.sent_cwd = 0
        self.received_ping = 0
        self.received_pong = 0
        self.received_cwd = 0

        self.received_pong_from_last_timeout = 0

        self.ping_yet_unpong = {}
        self.out_of_order_cnts = {}

        self.total_chunk_num = int(self.params.get('total_chunk_num', 0))

        self.resend_queue = OrderedDict()

        self.chunk_to_send = OrderedDict()
        for i in range(self.total_chunk_num):
            self.chunk_to_send[i] = 1


        self.completed_chunks = {}
        self.completed_chunk_offset = 0

        self.est_rtt = None
        self.dev_rtt = None
        self.rtt_alpha = self.params.get('rtt_alpha', 0.9)
        self.rtt_beta = self.params.get('rtt_beta', 0.9)

        self.ecn_enabled = False

        self.last_ssthresh_update_time = -1e3

        self.last_pong_received_time = None

        self.pong_timeout_threshold = self.params.get('timeout_threshold', 1)

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
                     path=self.path)

        pkt.ping_seq = self.ping_seq
        self.ping_seq += 1
        pkt.chunk_seq = chunk_seq

        self.sent_ping += 1

        self.ping_yet_unpong[pkt.chunk_seq] = pkt
        self.out_of_order_cnts[pkt.chunk_seq] = 0

        obj = pkt.get_next_hop()
        delay = random.random() * 0.05 * self.get_sending_interval()
        e = Event(cur_time + delay, obj, 'on_pkt_received', params=dict(pkt=pkt))

        print('#szx sent ping', pkt)
        return e, pkt

    def on_ping_sent(self):
        cur_time = self.get_cur_time()
        new_events = []

        self.check_expired(cur_time)
        if self.state != self.ONLINE:
            return new_events

        if self.can_send_ping() and len(self.ping_yet_unpong) < self.cwnd:
            if len(self.resend_queue) > 0:
                chunk_seq, _ = self.resend_queue.popitem(last=False)
                print('$ debug, resend chunk_seq', chunk_seq)
            elif len(self.chunk_to_send) > 0:
                #print('debug---xx', self.chunk_seq)
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
                #print('debug---zz sent', chunk_seq)

        e = Event(cur_time + self.get_sending_interval(), self, "on_ping_sent")
        new_events.append(e)
        return new_events

    def can_send_ping(self):
        return True

    def on_pkt_received(self, pkt):
        print('#szx on_pkt_received,pingpongflow', pkt.pkt_type)
        new_events = super().on_pkt_received(pkt)

        if pkt.pkt_type == self.PING_PKT_TYPE:
            lst = self.on_ping_received(pkt)
        elif pkt.pkt_type == self.PONG_PKT_TYPE:
            lst = self.on_pong_received(pkt)

        return new_events + lst

    def on_ping_received(self, pkt):
        self.received_ping += 1
        new_events = []
        cur_time = self.get_cur_time()

        if self.check_expired(cur_time):
            return new_events

        pkt.recv_time = cur_time

        pkt = self.change_ping_to_pong(pkt)

        obj = pkt.get_next_hop()
        e = Event(cur_time, obj, 'on_pkt_received', params=dict(pkt=pkt))
        new_events.append(e)

        self.sent_pong += 1
        return new_events

    def change_ping_to_pong(self, pkt):
        print('#szx change_ping_to_pong', pkt)
        """
        owd = pkt.recv_time - pkt.sent_time
        if self.stats is not None:
            stat = dict(owd=owd)
            self.stats.append((cur_time, stat))
        """
        pkt.pkt_type = self.PONG_PKT_TYPE
        pkt.size_in_bits = self.PONG_PKT_SIZE_IN_BITS

        pkt.ping_path = pkt.path
        pkt.ping_hop_cnt = pkt.hop_cnt

        pkt.path = self.reversed_path[-1 * pkt.ping_hop_cnt:]
        pkt.hop_cnt = 0

        # deal with ecn flags
        #self.receiver_deal_with_ce(pkt)

        #if self.check_completed(self.received_chunks):
        #    return []
        return pkt
    
    def change_ping_to_cwd(self, pkt):
        print('#szx change_ping_to_cwd', pkt)
        """
        owd = pkt.recv_time - pkt.sent_time
        if self.stats is not None:
            stat = dict(owd=owd)
            self.stats.append((cur_time, stat))
        """
        pkt.pkt_type = self.CWD_PKT_TYPE
        pkt.size_in_bits = self.CWD_PKT_SIZE_IN_BITS

        pkt.ping_path = pkt.path
        pkt.ping_hop_cnt = pkt.hop_cnt

        pkt.path = self.path[0]
        pkt.hop_cnt = 0

        # deal with ecn flags
        #self.receiver_deal_with_ce(pkt)

        #if self.check_completed(self.received_chunks):
        #    return []
        return pkt

    def on_cwd_received(self, pkt):
        print('#szx on_cwd_received', pkt)
        self.received_ping += 1
        new_events = []
        cur_time = self.get_cur_time()

        if self.check_expired(cur_time):
            return new_events

        pkt.recv_time = cur_time

        pkt = self.change_ping_to_cwd(pkt)

        obj = pkt.get_next_hop()
        e = Event(cur_time, obj, 'on_pkt_received', params=dict(pkt=pkt))
        new_events.append(e)

        self.sent_pong += 1
        return new_events

    def receiver_deal_with_ce(self, pkt):
        pass

    def update_est_rtt(self, sample_rtt):
        print('#szx update_est_rtt',self, sample_rtt)
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

    def on_pong_received(self, pkt):
        print('#szx on_pong_received', pkt)
        self.received_pong += 1

        if self.state != self.ONLINE:
            return []

        self.received_pong_from_last_timeout += 1
        #print('# get pong', pkt.ping_seq, pkt.chunk_seq, 
        # 'total', self.received_pong, self.completed_chunk_offset,
        #  len(self.completed_chunks), self.total_chunk_num, len(self.ping_yet_unpong))

        cur_time = self.get_cur_time()
        self.last_pong_received_time = cur_time
        sample_rtt = cur_time - pkt.sent_time
        self.update_est_rtt(sample_rtt)
        new_chunk_seq = getattr(pkt, 'new_chunk_seq', -1)
        # Newly added
        self.relocated_chunk_seq = new_chunk_seq
        if new_chunk_seq > self.chunk_seq and new_chunk_seq in self.chunk_to_send:
            self.chunk_seq = new_chunk_seq

        self.pong_path_len = len(pkt.path)

        #print('# get pong chunk', cur_time, self.id, pkt.chunk_seq, self.completed_chunk_offset, len(self.completed_chunks), self.total_chunk_num)

        #self.acked_offsets.update(pkt.acked_offsets)

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
            #print('#', cur_time, 'to_resend', len(to_resend), to_resend)

        # TODO: xxx
        if self.cc_state == self.CC_STATE_CONGESTION_AVOIDANCE:
            if len(to_resend) > 0:
                if cur_time > self.last_ssthresh_update_time + self.est_rtt:
                    self.last_ssthresh_update_time = cur_time
                    cwnd = max(1, self.cwnd * 0.5)
                    self.ssthresh = cwnd
                    self.update_cwnd(cwnd)
                    #print('#', cur_time, 'ssthresh', self.ssthresh, 'to_resend', len(to_resend), to_resend)
            else:
                self.update_cwnd(self.cwnd + 1. / self.cwnd)

        elif self.cc_state == self.CC_STATE_SLOW_START:
            if len(to_resend) > 0:
                # switch to congestion avoidance
                self.cc_state = self.CC_STATE_CONGESTION_AVOIDANCE
                self.last_ssthresh_update_time = cur_time

                cwnd = max(1, self.cwnd * 0.5)
                self.ssthresh = cwnd
                self.update_cwnd(cwnd)
                #print('# Flow', self.id, 'ssthresh', self.ssthresh, '# from SLOW_START to CONGESTION_AVOIDANCE')
            else:
                # stay in slow start
                self.update_cwnd(self.cwnd + 1)
        else:
            print('# error!')
            raise ValueError

        return []

    def update_cwnd(self, new_cwnd):
        print('#szx update_cwnd', self, new_cwnd)
        self.cwnd = max(self.min_cwnd, min(self.max_cwnd, new_cwnd))
        self.gen_stat()

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
            rate_pps=self.cwnd / self.est_rtt,
            ping_seq=self.ping_seq,
            chunk_seq=self.chunk_seq,
            ping_yet_unpong=len(self.ping_yet_unpong),
            sent_ping=self.sent_ping,
            sent_pong=self.sent_pong,
            received_ping=self.received_ping,
            received_pong=self.received_pong,
            #lost=self.total_lost,
            relocated_chunk_seq=self.relocated_chunk_seq,
            pong_path_len=self.pong_path_len,
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

#这里提供的 PingPongFlow 类中，ping 和 pong 数据包的使用模拟了一种网络流量控制和拥塞检测机制。
#ping 数据包用于发送请求，检测网络的传输延迟；而 pong 数据包则作为响应返回，帮助流量控制算法根据 RTT（往返时间）调整拥塞窗口（cwnd）等参数。
#通过这种方式，ping 和 pong 数据包模拟了类似 TCP 协议中的流量控制和拥塞管理过程，帮助优化数据的发送和接收，确保网络资源的有效利用。


#MDP 的 PING 数据包较小（2位），而 PONG 数据包较大（8 KB）。
#MUP 的 PING 数据包较大（8 KB），而 PONG 数据包较小（2位）。
#MDP 和 MUP 都属于 PingPongFlow 流量模型，但是它们在数据包的大小和功能语义上存在明显区别。
# MDP 更倾向于小型的请求和大响应，而 MUP 可能用于较大请求和小响应的场景。
class MDP(PingPongFlow):
    TYPE = 'MDP'
    PING_PKT_TYPE = 'MDP_PING'
    PONG_PKT_TYPE = 'MDP_PONG'
    CWD_PKT_TYPE = 'MDP_CWD'

    PING_PKT_SIZE_IN_BITS = 2
    PONG_PKT_SIZE_IN_BITS = 8 * 1024


# class MUP(PingPongFlow):
#     TYPE = 'MUP'
#     PING_PKT_TYPE = 'MUP_PING'
#     PONG_PKT_TYPE = 'MUP_PONG'

#     PING_PKT_SIZE_IN_BITS = 8 * 1024
#     PONG_PKT_SIZE_IN_BITS = 2
#szx修改后
class MUP(PingPongFlow):
    TYPE = 'MUP'
    PING_PKT_TYPE = 'MUP_PING'
    PONG_PKT_TYPE = 'MUP_PONG'
    CWD_PKT_TYPE = 'MUP_CWD'

    PING_PKT_SIZE_IN_BITS = 8 * 1024
    PONG_PKT_SIZE_IN_BITS = 2
    CWD_PKT_SIZE_IN_BITS = 2

#szx修改前
# class MUP(PingPongFlow):
    
#     def __init__(self, type='MUP', ping_pkt_type='MUP_PING', pong_pkt_type='MUP_PONG',cwd_pkt_type='CWD',
#                  ping_pkt_size_in_bits=8 * 1024, pong_pkt_size_in_bits=2):
#         self.TYPE = type
#         self.PING_PKT_TYPE = ping_pkt_type
#         self.PONG_PKT_TYPE = pong_pkt_type
#         self.CWD_PKT_TYPE =  cwd_pkt_type
#         self.PING_PKT_SIZE_IN_BITS = ping_pkt_size_in_bits
#         self.PONG_PKT_SIZE_IN_BITS = pong_pkt_size_in_bits



class MDPCache(object):
    #你提供的 MDPCache 类实现了一个缓存系统，用于管理与任务（job_id）和数据块（chunk_id）相关的缓存数据。
    # 缓存有一个总容量限制 (max_total_cache_size)，并且每个任务的缓存也可以有限制 (max_per_job_cache_size)。
    # 在该类中，主要通过对 job_id 和 chunk_id 进行管理，更新缓存和维护每个任务的最大块序列。
    def __init__(self, max_total_cache_size, max_per_job_cache_size=None):
        self.max_total_cache_size = max_total_cache_size
        self.max_per_job_cache_size = max_per_job_cache_size
        #每个任务最大缓存量，表示每个任务缓存的块数量上限（如果没有指定则默认为 None）。
        self.data = {}
        self.data_flat = OrderedDict()
        self.max_chunk_seq = {}

    def move_to_end(self, job_id, chunk_id):
        self.data[job_id].move_to_end(chunk_id)
        self.data_flat.move_to_end((job_id, chunk_id))
    #该方法用于将某个特定的 job_id 和 chunk_id 对应的数据块移到缓存的末尾。
    # 这里使用了 OrderedDict 的 move_to_end() 方法，这可以让数据的顺序保持不变，便于实现最近最少使用（LRU）缓存淘汰策略。

    def has_cache(self, job_id, chunk_id):
        #用于检查某个 job_id 和 chunk_id 是否在缓存中。
        # 如果缓存中不存在该数据块（job_id, chunk_id），则返回 False；否则返回 True。
        return (job_id, chunk_id) in self.data_flat
        if job_id not in self.data:
            return False
        else:
            return chunk_id in self.data[job_id]

    def update_cache(self, job_id, chunk_id, cur_time):
        #更新缓存中的数据。根据当前时间（cur_time）更新 job_id 和 chunk_id 对应的数据块。
        #在每次缓存更新时，会更新 data_flat 和 data，并检查是否超出总缓存容量。如果超出容量，淘汰最旧的数据块。
        #另外，max_chunk_seq[job_id] 用来记录每个任务的最大块序列号，若当前的 chunk_id 更大，则更新它。
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
    #你的 EdgeBox 类继承自 Middlebox 类，实现了用于处理 MDP 和 MUP 类型数据包的处理逻辑。
    # EdgeBox 的功能包括管理缓存、处理超时和溢出、以及任务的流量控制。
    STOPPED = 0
    RUNNING = 1

    def __init__(
            self,
            mdp_cache_algorithm=None,
            mup_cache_algorithm=None,
            max_mdp_cache_size=1e6,  # 1GB
            max_mup_cache_size=1e6,
            mup_timeout=10,
            enabled=True,
            jobs_config={},
            relocation_enabled=False):
        super().__init__()
        #初始化了 EdgeBox 实例的各种配置，包括缓存管理、超时配置、任务配置等。
        self.nfs = {}
        #nfs 是一个字典，映射了 MDP 和 MUP 类型到相应的处理函数 process_mdp 和 process_mup。
        self.nfs[MDP.TYPE] = self.process_mdp
        self.nfs[MUP.TYPE] = self.process_mup

        self.mdp_cache = MDPCache(max_mdp_cache_size)  #OrderedDict()
        #mdp_cache 和 mup_cache 使用了 MDPCache 来管理缓存。
        self.mdp_perf_metrics = dict(
            total={},
            detail={},
        )
        self.mup_cache = MDPCache(max_mdp_cache_size)  # OrderedDict() # TODO: update impl
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
        #停止 EdgeBox 的工作，设置状态为 STOPPED。
        self.state = self.STOPPED
        """
        for k in self.mup_cache:
            self.add_aggregated_to_send_queue(chunk_key=k, reason='timeout')
        self.mup_cache.clear()
        """

    def register_ebmup(self, job_id, f):
        #注册 job_id 对应的 ebmup 流。
        self.ebmup_flows[job_id] = f

    def process(self, pkt):
        print('#szx process', pkt)
        #处理传入的 pkt，如果 EdgeBox 启用，将根据数据包类型调用相应的处理函数（process_mdp 或 process_mup）。
        self.perf_metrics['received'][pkt.pkt_type] = self.perf_metrics['received'].get(pkt.pkt_type, 0) + 1
        if self.enabled:
            pkt = self.nfs[pkt.flow.TYPE](pkt)

        self.perf_metrics['sent'][pkt.pkt_type] = self.perf_metrics['sent'].get(pkt.pkt_type, 0) + 1
        return pkt

    def change_ping_to_pong(self, pkt):
        #将 ping 包更改为 pong 包。该方法用于 MDP 和 MUP 类型数据包的转换。

        return pkt.flow.change_ping_to_pong(pkt)
    
    def change_ping_to_cwd(self, pkt):
        #将 ping 包更改为cwd 包。

        return pkt.flow.change_ping_to_cwd(pkt)

    def process_mdp(self, pkt):
        #处理 MDP 类型数据包。根据数据包类型（PING 或 PONG）进行相应处理：
        #return pkt
        job_id = pkt.flow.job_id
        flow_id = pkt.flow.id

        self.flows_of_each_job.setdefault(job_id, {})[flow_id] = 1
        #chunk_key = (job_id, pkt.chunk_seq)
        cur_time = self.get_cur_time()

        if pkt.pkt_type == MDP.PING_PKT_TYPE:
            if self.mdp_cache.has_cache(job_id, pkt.chunk_seq):
                #PING: 如果缓存中已经存在该数据块，则更新缓存并转换为 PONG。
                self.mdp_cache.move_to_end(job_id, pkt.chunk_seq)
                pkt = self.change_ping_to_pong(pkt)
                self.mdp_perf_metrics['total'][job_id] = self.mdp_perf_metrics['total'].setdefault(job_id, 0) + 1
                self.mdp_perf_metrics['detail'][flow_id] = self.mdp_perf_metrics['detail'].setdefault(flow_id, 0) + 1

        elif pkt.pkt_type == MDP.PONG_PKT_TYPE:
            #PONG: 更新缓存，并在启用迁移时获取新的块序列号。
            #pkt.new_chunk_seq = self.mdp_cache.update_cache_and_get_new_chunk_seq(
            #    job_id, pkt.chunk_seq, cur_time, self.relocation_enabled)
            self.mdp_cache.update_cache(job_id, pkt.chunk_seq, cur_time)
            if self.relocation_enabled:
                pkt.new_chunk_seq = self.mdp_cache.get_relocation_seq(job_id, pkt.chunk_seq)
        else:
            raise ValueError
        return pkt

    def register_env(self, env):
        self.env = env  # 注册 net 实例
    def enable_remove_timeout_mupchunk(self):
        #启用超时 MUP 数据块的删除。如果超时数据块存在，调用 remove_timeout_mupchunk() 删除并加入事件队列。
        if self.remove_timeout_mupchunk_running:
            return
        else:
            events = self.remove_timeout_mupchunk()
            self.env.add_events(events)

    # def remove_timeout_mupchunk(self):
    #     # remove timeouted
    #     #检查缓存中是否有超时的 MUP 数据块，并将其从缓存中移除。
    #     # 如果缓存为空，停止删除任务；否则，重新加入事件。
    #     cur_time = self.get_cur_time()
    #     to_remove = []
    #     t_treshold = cur_time - self.mup_timeout
    #     for k in self.mup_cache:
    #         if self.mup_cache[k]['t'] < t_treshold:
    #             # send it to FLS then del cache
    #             to_remove.append(k)
    #         else:
    #             break

    #     for k in to_remove:
    #         del self.mup_cache[k]
    #         self.add_aggregated_to_send_queue(chunk_key=k, reason='timeout')

    #     new_events = []
    #     if len(self.mup_cache) == 0:
    #         self.remove_timeout_mupchunk_running = False
    #         return new_events
    #     else:
    #         e = Event(cur_time + self.mup_timeout, self, "remove_timeout_mupchunk")
    #         new_events.append(e)
    #         return new_events
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




    def add_aggregated_to_send_queue(self, chunk_key, chunk=None, reason=None):
        print('#szx add_aggregated_to_send_queue', chunk_key, chunk, reason)
        #将缓存中的数据块加入发送队列。如果数据块超时或完成，将触发发送。
        # TODO: add an mup flow to handle these packets
        """
        self.mup_perf_metrics['total'] += 1
        self.mup_perf_metrics['detail'][reason] = 1 + self.mup_perf_metrics['detail'].get(reason, 0)
        self.perf_metrics['sent'][MUP.PING_PKT_TYPE] = 1 + self.perf_metrics['sent'].get(MUP.PING_PKT_TYPE, 0)
        self.perf_metrics['received'][MUP.PONG_PKT_TYPE] = 1 + self.perf_metrics['received'].get(MUP.PONG_PKT_TYPE, 0)
        """
        job_id, chunk_id = chunk_key
        f = self.ebmup_flows[job_id]
        f.add_to_ping_queue(chunk_id, chunk)

    def process_mup(self, pkt):
        #return pkt
        #处理 MUP 类型数据包。与 MDP 类似，根据数据包类型进行处理
        job_id = pkt.flow.job_id
        flow_id = pkt.flow.id

        chunk_key = (job_id, pkt.chunk_seq)

        cur_time = self.get_cur_time()

        if pkt.pkt_type == MUP.PING_PKT_TYPE:
            #PING: 更新缓存，如果 MUP 数据包满足条件，则将其标记为已完成或溢出
            if flow_id not in self.mup_cache_meta.get(chunk_key, []):
                # ignore duplicated packet
                self.mup_cache_meta.setdefault(chunk_key, set()).add(flow_id)
                # remove timeouted
                """
                to_remove = []
                for k in self.mup_cache:
                    if self.mup_cache[k]['t'] + self.mup_timeout < cur_time:
                        # send it to FLS then del cache
                        to_remove.append(k)
                    else:
                        break

                for k in to_remove:
                    del self.mup_cache[k]
                    self.add_aggregated_to_send_queue(chunk_key=k, reason='timeout')
                """
                # if chunk_key not in self.mup_cache:
                #     self.mup_cache[chunk_key] = dict(t=cur_time, data=None)
                #     self.enable_remove_timeout_mupchunk()
                # else:
                #     # TODO: update cached chunk
                #     pass
                if not self.mup_cache.has_cache(job_id, pkt.chunk_seq):
                    self.mup_cache.update_cache(job_id, pkt.chunk_seq, cur_time)
                    self.enable_remove_timeout_mupchunk()
                else:
                    # TODO: update cached chunk
                       # 更新缓存中的时间戳或其他元数据
                    # self.mup_cache.update_cache(job_id, pkt.chunk_seq, cur_time)

                    # # 调整优先级（将块移动到末尾）
                    # self.mup_cache.move_to_end(job_id, pkt.chunk_seq)
                    pass


                if len(self.mup_cache_meta[chunk_key]) >= self.jobs_config[job_id]['flownum']:
                    # remove completed
                    del self.mup_cache[chunk_key]
                    self.add_aggregated_to_send_queue(chunk_key=chunk_key, reason='completed')
                   

                elif len(self.mup_cache) > self.max_mup_cache_size:
                    # remove oldest in case of overflow
                    k, _ = self.mup_cache.popitem(last=False)
                    self.add_aggregated_to_send_queue(chunk_key=k, reason='overflow')
                    
            pkt = self.change_ping_to_pong(pkt)

        elif pkt.pkt_type == MUP.PONG_PKT_TYPE:
            #PONG: 目前抛出 ValueError，因为不应接收到 PONG 类型的 MUP 数据包。
            raise ValueError
        else:
            raise ValueError

        #print('pkt.type', pkt.pkt_type, pkt.path, pkt.ping_seq, pkt.chunk_seq)
        return pkt


class EBMUP(MUP):

    TYPE = "EBMUP"

    def add_to_ping_queue(self, chunk_key=None, chunk=None):
        #这个方法将一个新的 ping 请求添加到 ping 队列中，同时启动重新启动过程并添加事件。
        #该方法更新了 total_chunk_num（总的 chunk 数量）并触发了事件调度。
        self.chunk_to_send[self.total_chunk_num] = 1
        self.total_chunk_num += 1

        events = self.restart()
        self.env.add_events(events)

        cur_time = self.get_cur_time()
        #print('add to ping queue', cur_time, self.total_chunk_num, events)

    """
    def on_ping_received(self, pkt):
        cur_time = self.get_cur_time()
        print('# flow', self.id, 'gets ping of', pkt.chunk_seq, 'at time', cur_time, pkt.sent_time)
        return super().on_ping_received(pkt)


    def on_pong_received(self, pkt):
        cur_time = self.get_cur_time()
        print('# flow', self.id, 'gets pong of', pkt.chunk_seq, 'at time', cur_time, pkt.sent_time)
        return super().on_pong_received(pkt)
    """


def reset_obj_ids():
    #重置对象ID。
    #这个方法将 Link、Flow 和 Middlebox 类的 _next_id 重置为 1，通常用于确保在测试中每个对象的 ID 从 1 开始，避免重复。
    Link._next_id = 1
    Flow._next_id = 1
    Middlebox._next_id = 1


def run_test(
    #该方法用于创建和运行一个模拟网络环境。
    DeviceNum=2,
    Device_used_ratio=1,
    JobNum=1,
    FCLS=MDP,
    bw1_bps=100e6,
    bw2_bps=100e6,
    rtt1=10 / 1e3,
    rtt2=40 / 1e3,
    lr1=0,
    lr2=0,
    MODEL_SIZE_IN_CHUNK=50e3,
    max_mdp_cache_size=1e4,
    max_mup_cache_size=1e4,
    enabled_box=True,
    qsize_alpha=1.0,
    flow_start_timer=lambda: 0,
):

    print("run_test")
    reset_obj_ids()

    jobs_config = {}
    for i in range(JobNum):
        jobs_config[i] = dict(flownum=max(1, int(DeviceNum * Device_used_ratio)))

    bdp_in_bits = int(bw1_bps * rtt1) + int(bw2_bps * rtt2)
    qsize_in_bits = bdp_in_bits * qsize_alpha

    weights = {1: 8, 2: 4, 3: 2, 4: 1}
    mq_sizes = {i: qsize_in_bits for i in weights}
    red_configs = {i: dict(MinTh_r=0.4, MaxTh_r=0.9, UseEcn=False, UseHardDrop=False) for i in mq_sizes}
    qdisc = DWRR(mq_sizes, red_configs, weights=weights)

    box = EdgeBox(
        jobs_config=jobs_config,
        max_mdp_cache_size=max_mdp_cache_size,  # 1GB
        max_mup_cache_size=max_mup_cache_size,
        enabled=enabled_box,
    )

    links = [Link(bw1_bps, rtt1 / 2, qdisc.copy(), lr1) for _ in range(2 * DeviceNum)]

    links += [Link(bw2_bps, rtt2 / 2, qdisc.copy(), lr2) for _ in range(2)]

    flows = []

    for i in range(JobNum):
        min_start_time = None
        for j in random.sample(range(DeviceNum), jobs_config[i]['flownum']):
            start_time = flow_start_timer()
            if min_start_time is None or start_time > min_start_time:
                min_start_time = start_time
            f = FCLS(
                path=[
                    links[2 * j],
                    box,
                    links[-2],
                ],
                rpath=[
                    links[-1],
                    box,
                    links[2 * j + 1],
                ],
                nic_rate_pps=2 * bw1_bps,
                job_id=i,
                start_time=start_time,
                #expired_time=20,
                total_chunk_num=MODEL_SIZE_IN_CHUNK,
            )
            flows.append(f)

        if FCLS == MUP:
            # make mup flow from EB to FLS
            f = EBMUP(
                path=[
                    links[-2],
                ],
                rpath=[
                    links[-1],
                ],
                nic_rate_pps=2 * bw2_bps,
                job_id=i,
                start_time=min_start_time,
                #expired_time=100,
                total_chunk_num=0,
            )
            flows.append(f)
            box.register_ebmup(job_id=i, f=f)

    net = Environment(flows, links, boxes=[
        box,
    ])

    net.run()

    results = {}
    for f in flows:
        if f.job_id not in results:
            results[f.job_id] = {}

        results[f.job_id][f.id] = dict(sent_ping=f.sent_ping,
                                       received_ping=f.received_ping,
                                       sent_pong=f.sent_pong,
                                       received_pong=f.received_pong,
                                       start_time=f.start_time,
                                       completed_time=f.last_pong_received_time,
                                       ftype=f.TYPE)

    return results


if __name__ == '__main__':
    print("szx inp main")
    for FCLS in (MDP, ):  #MUP
        for enabled_box in (True, False):
            results = run_test(DeviceNum=3, JobNum=1, FCLS=FCLS, MODEL_SIZE_IN_CHUNK=3e3, max_mup_cache_size=1e3, enabled_box=enabled_box)

            print()
            print('#', FCLS.__name__, 'Box', enabled_box)
            #print(inst.boxes[0].mdp_perf_metrics)
            #print(inst.boxes[0].mup_perf_metrics)
            #print(inst.boxes[0].perf_metrics)
            #print(inst.stat)
            print(results)

    print('done!')