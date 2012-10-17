'''
Target monitoring via SSH
'''

from collections import defaultdict
from lxml import etree
from subprocess import PIPE, Popen
import base64
import logging
import os.path
import re
import select
import signal
import sys
import tempfile
import time

# FIXME: 2 synchronize times between agent and collector better
class Config(object):
    '''
    Config reader helper
    '''
    def __init__(self, config):
        self.tree = etree.parse(config)

    def loglevel(self):
        '''Get log level from config file. Possible values: info, debug'''

        log_level = 'info'
        log_level_raw = self.tree.xpath('/Monitoring')[0].get('loglevel')
        if log_level_raw in ('info', 'debug'):
            log_level = log_level_raw
        return log_level

class SSHWrapper:
    '''
    separate SSH calls to be able to unit test the collector
    '''
    def __init__(self):
        self.log = logging.getLogger(__name__)
        self.ssh_opts = ['-q', '-o', 'StrictHostKeyChecking=no', '-o', 'PasswordAuthentication=no', '-o', 'NumberOfPasswordPrompts=0', '-o', 'ConnectTimeout=5']
        self.scp_opts = []        
        self.host = None
        self.port = None

    def set_host_port(self, host, port):
        '''
        Set host and port to use
        '''
        self.host = host
        self.port = port
        self.scp_opts = self.ssh_opts + ['-P', self.port]
        self.ssh_opts = self.ssh_opts + ['-p', self.port]

    def get_ssh_pipe(self, cmd):
        '''
        Get open ssh pipe 
        '''
        args = ['ssh'] + self.ssh_opts + [self.host] + cmd
        self.log.debug('Executing: %s', args)
        return Popen(args, stdout=PIPE, stderr=PIPE, stdin=PIPE, bufsize=0, preexec_fn=os.setsid)

    def get_scp_pipe(self, cmd):
        '''
        Get open scp pipe 
        '''
        args = ['scp'] + self.scp_opts + cmd
        self.log.debug('Executing: %s', args)
        return Popen(args, stdout=PIPE, stderr=PIPE, stdin=PIPE, bufsize=0, preexec_fn=os.setsid)



class AgentClient(object):
    '''
    Agent client connection
    '''
    
    def __init__(self, **kwargs):
        self.run = []
        self.host = None
        
        self.port = 22
        for key, value in kwargs.iteritems():
            setattr(self, key, value)
        self.ssh = None

        temp_config = tempfile.mkstemp('.cfg', 'agent_')
        self.path = {
            # Destination path on remote host
            'AGENT_REMOTE_FOLDER': '/var/tmp/lunapark_monitoring',

            # Source path on tank
            'AGENT_LOCAL_FOLDER': os.path.dirname(__file__) + '/agent/',
            'METRIC_LOCAL_FOLDER': os.path.dirname(__file__) + '/agent/metric',

            # Temp config path
            'TEMP_CONFIG': temp_config[1]
        }
        self.interval = None
        self.metric = None
        self.custom = None
        self.python = None

    def start(self):
        '''
        Start remote agent
        '''
        logging.debug('Start monitoring: %s' % self.host)
        if not self.run:
            raise ValueError("Empty run string")
        self.run += ['-t', str(int(time.time()))]
        logging.debug(self.run)
        pipe = self.ssh.get_ssh_pipe(self.run)
        logging.debug("Started: %s", pipe)
        return pipe


    def create_agent_config(self, loglevel):
        ''' Creating config '''
        cfg = open(self.path['TEMP_CONFIG'], 'w')
        cfg.write('[main]\ninterval=%s\n' % self.interval)
        cfg.write('host=%s\n' % self.host)
        cfg.write('loglevel=%s\n' % loglevel)
        cfg.write('[metric]\nnames=%s\n' % self.metric)
        cfg.write('[custom]\n')
        for method in self.custom:
            if self.custom[method]:
                cfg.write('%s=%s\n' % (method, ','.join(self.custom[method])))
        
        cfg.close()
        return self.path['TEMP_CONFIG']

    def install(self, loglevel):
        """ Create folder and copy agent and metrics scripts to remote host """
        logging.info("Installing monitoring agent at %s...", self.host)
        agent_config = self.create_agent_config(loglevel)
        
        self.ssh.set_host_port(self.host, self.port)

        # getting remote temp dir
        cmd = [self.python + ' -c "import tempfile; print tempfile.mkdtemp();"']
        logging.debug("Get remote temp dir: %s", cmd)
        pipe = self.ssh.get_ssh_pipe(cmd)

        err = pipe.stderr.read().strip()
        if err:
            raise RuntimeError("[%s] ssh error: '%s'" % (self.host, err))
        pipe.wait()
        logging.debug("Return code [%s]: %s" % (self.host, pipe.returncode))
        if pipe.returncode:
            raise RuntimeError("Failed to get remote dir via SSH at %s, code %s: %s" % (self.host, pipe.returncode, pipe.stdout.read().strip()))

        remote_dir = pipe.stdout.read().strip()
        if (remote_dir):
            self.path['AGENT_REMOTE_FOLDER'] = remote_dir
        logging.debug("Remote dir at %s:%s", self.host, self.path['AGENT_REMOTE_FOLDER']);

        # Copy agent
        cmd = ['-r', self.path['AGENT_LOCAL_FOLDER'], self.host + ':' + self.path['AGENT_REMOTE_FOLDER']]
        logging.debug("Copy agent to %s: %s" % (self.host, cmd))

        pipe = self.ssh.get_scp_pipe(cmd)
        pipe.wait()
        logging.debug("AgentClient copy exitcode: %s", pipe.returncode)
        if pipe.returncode != 0:
            raise RuntimeError("AgentClient copy exitcode: %s" % pipe.returncode)

        # Copy config
        cmd = [self.path['TEMP_CONFIG'], self.host + ':' + self.path['AGENT_REMOTE_FOLDER'] + '/agent.cfg']
        logging.debug("[%s] Copy config: %s", cmd, self.host)
            
        pipe = self.ssh.get_scp_pipe(cmd)
        pipe.wait()
        logging.debug("AgentClient copy config exitcode: %s", pipe.returncode)
        if pipe.returncode != 0:
            raise RuntimeError("AgentClient copy config exitcode: %s" % pipe.returncode)

        if os.getenv("DEBUG") or 1:
            debug = "DEBUG=1"
        else:
            debug = ""
        self.run = ['/usr/bin/env', debug, self.python, self.path['AGENT_REMOTE_FOLDER'] + '/agent/agent.py', '-c', self.path['AGENT_REMOTE_FOLDER'] + '/agent.cfg']
        return agent_config

    def uninstall(self):
        """ Remove agent's files from remote host"""
        log_file = tempfile.mkstemp('.log', "agent_" + self.host + "_")[1]
        cmd = [self.host + ':' + self.path['AGENT_REMOTE_FOLDER'] + "_agent.log", log_file]
        logging.debug("Copy agent log from %s: %s" , self.host, cmd)
        remove = self.ssh.get_scp_pipe(cmd)
        remove.wait()
        
        logging.info("Removing agent from: %s...", self.host)
        cmd = ['rm', '-r', self.path['AGENT_REMOTE_FOLDER']]
        remove = self.ssh.get_ssh_pipe(cmd)
        remove.wait()
        return log_file

class MonitoringCollector:
    '''
    Class to aggregate data from several collectors
    '''
    def __init__(self):
        self.log = logging.getLogger(__name__)
        self.config = None
        self.default_target = None
        self.agents = []
        self.agent_pipes = []
        self.filter_conf = {}
        self.listeners = []
        self.ssh_wrapper_class = SSHWrapper
        self.first_data_received = False
        self.send_data = ''
        self.artifact_files = []
        self.inputs, self.outputs, self.excepts = [], [], []

    def add_listener(self, obj):
        '''         Add data line listener        '''
        self.listeners.append(obj)

    def prepare(self): 
        ''' Prepare for monitoring - install agents etc'''       
        # Parse config
        agent_config = []
        if self.config:
            [agent_config, self.filter_conf] = self.getconfig(self.config, self.default_target)

        self.log.debug("filter_conf: %s", self.filter_conf)        
        conf = Config(self.config)
        
        # Filtering
        self.filter_mask = defaultdict(str)
        for host in self.filter_conf:
            self.filter_mask[host] = []
        self.log.debug("Filter mask: %s", self.filter_mask)
            
        # Creating agent for hosts
        logging.debug('Creating agents')
        for adr in agent_config:
            logging.debug('Creating agent: %s', adr)
            agent = AgentClient(**adr)
            agent.ssh = self.ssh_wrapper_class()
            self.agents.append(agent)
        
        # Mass agents install
        logging.debug("Agents: %s", self.agents)
        
        for agent in self.agents:
            logging.debug('Install monitoring agent. Host: %s', agent.host)
            self.artifact_files.append(agent.install(conf.loglevel()))        

    def start(self):        
        ''' Start N parallel agents ''' 
        for a in self.agents:
            pipe = a.start()
            self.agent_pipes.append(pipe)
            self.outputs.append(pipe.stdout)
            self.excepts.append(pipe.stderr)     
            
        logging.debug("Pipes: %s", self.agent_pipes)
        
    def poll(self):
        '''
        Poll agents for data
        '''
        readable, writable, exceptional = select.select(self.outputs, self.inputs, self.excepts, 0)
        logging.debug("Streams: %s %s %s", readable, writable, exceptional)
        
        # if empty run - check children
        if (not readable) or exceptional:
            for pipe in self.agent_pipes:
                if pipe.returncode:
                    logging.debug("Child died returncode: %s", pipe.returncode)
                    self.outputs.remove(pipe.stdout)
                    self.agent_pipes.remove(pipe)
        
        # Handle exceptions
        for s in exceptional:
                data = s.readline()
                while data:
                    logging.error("Got exception [%s]: %s", s, data)
                    data = s.readline()                
    
        while readable:
            s = readable.pop(0)
            # Handle outputs
            data = s.readline()
            if not data:
                continue
            logging.debug("Got data from agent: %s", data.strip())    
            self.send_data += self.filter_unused_data(self.filter_conf, self.filter_mask, data)
            logging.debug("Data after filtering: %s", self.send_data)

        if not self.first_data_received and self.send_data:
            self.first_data_received = True
            self.log.info("Monitoring received first data")
        else:
            for listener in self.listeners:
                listener.monitoring_data(self.send_data)
            self.send_data = ''
            
        return len(self.outputs)            
    
    def stop(self):
        ''' Shutdown  agents       '''
        logging.debug("Initiating normal finish")
        for pipe in self.agent_pipes:
            if pipe.pid:
                logging.debug("Killing %s with %s", pipe.pid, signal.SIGINT)
                os.kill(pipe.pid, signal.SIGINT)

        for agent in self.agents:
            self.artifact_files.append(agent.uninstall())
        
    def getconfig(self, filename, target_hint):
        ''' Prepare config data'''
        default = {
            'System': 'csw,int',
            'CPU': 'user,system,iowait',
            'Memory': 'free,used',
            'Disk': 'read,write',
            'Net': 'recv,send',
        }
    
        default_metric = ['CPU', 'Memory', 'Disk', 'Net']
    
        try:
            tree = etree.parse(filename)
        except IOError, e:
            logging.error("Error loading config: %s", e)
            raise RuntimeError ("Can't read monitoring config %s" % filename)
    
        hosts = tree.xpath('/Monitoring/Host')
        names = defaultdict()
        config = []
        hostname = ''
        filter_obj = defaultdict(str)
        for host in hosts:
            hostname = host.get('address')
            if hostname == '[target]':
                if not target_hint:
                    raise ValueError("Can't use [target] keyword with no target parameter specified")
                logging.debug("Using target hint: %s", target_hint)
                hostname = target_hint
            stats = []
            custom = {'tail': [], 'call': [], }
            metrics_count = 0
            for metric in host:
                # known metrics
                if metric.tag in default.keys():
                    metrics_count += 1
                    m = default[metric.tag].split(',')
                    if metric.get('measure'):
                        m = metric.get('measure').split(',')
                    for el in m:
                        if not el:
                            continue;
                        stat = "%s_%s" % (metric.tag, el)
                        stats.append(stat)
                        agent_name = self.get_agent_name(metric.tag, el)
                        if agent_name:
                            names[agent_name] = 1
                # custom metric ('call' and 'tail' methods)
                if (str(metric.tag)).lower() == 'custom':
                    metrics_count += 1
                    isdiff = metric.get('diff')
                    if not isdiff:
                        isdiff = 0
                    stat = "%s:%s:%s" % (base64.b64encode(metric.get('label')), base64.b64encode(metric.text), isdiff)
                    stats.append('Custom:' + stat)
                    custom[metric.get('measure')].append(stat)
    
            logging.debug("Metrics count: %s", metrics_count)
            logging.debug("Host len: %s", len(host))
            logging.debug("keys: %s", host.keys())
            logging.debug("values: %s", host.values())
    
            # use default metrics for host
            if metrics_count == 0:
                for metric in default_metric:
                    m = default[metric].split(',')
                    for el in m:
                        stat = "%s_%s" % (metric, el)
                        stats.append(stat)
                        agent_name = self.get_agent_name(metric, el)
                        if agent_name:
                            names[agent_name] = 1
    
            metric = ','.join(names.keys())
            tmp = {}
    
            if metric:
                tmp.update({'metric': metric})
            else:
                tmp.update({'metric': 'cpu-stat'}) 
    
            if host.get('interval'):
                tmp.update({'interval': host.get('interval')})
            else:
                tmp.update({'interval': 1})
                    
            if host.get('priority'):
                tmp.update({'priority': host.get('priority')})
            else:
                tmp.update({'priority': 0})
    
            if host.get('port'):
                tmp.update({'port': host.get('port')})
            else:
                tmp.update({'port': '22'})
    
            if host.get('python'):
                tmp.update({'python': host.get('python')})
            else:
                tmp.update({'python': '/usr/bin/python'})
                
    
            tmp.update({'custom': custom})
    
            tmp.update({'host': hostname})
            filter_obj[hostname] = stats
            config.append(tmp)
    
        return [config, filter_obj]
    
    def filtering(self, mask, filter_list):
        ''' Filtering helper '''
        host = filter_list[0]
        initial = [0, 1]
        out = ''
        res = []
        if mask[host]:
            keys = initial + mask[host]
            for key in keys:
                try:
                    res.append(filter_list[key])
                    out += filter_list[key] + ';'
                except IndexError:
                    self.log.warn("Problems filtering data: %s with %s", mask, len(filter_list))
                    return None
        return ';'.join(res)
            
    def filter_unused_data(self, filter_conf, filter_mask, data):
        ''' Filter unselected metrics from data '''
        self.log.debug("Filtering data: %s", data)
        out = ''
        # Filtering data
        keys = data.rstrip().split(';')
        if re.match('^start;', data): # make filter_conf mask
            host = keys[1]
            for i in xrange(3, len(keys)):
                if keys[i] in filter_conf[host]:
                    filter_mask[host].append(i - 1)
            self.log.debug("Filter mask: %s", filter_mask)
            out = 'start;'
            out += self.filtering(filter_mask, keys[1:]).rstrip(';') + '\n'
        elif re.match('^\[debug\]', data): # log debug output
            logging.debug('agent debug: %s', data.rstrip())
        else:
            filtered = self.filtering(filter_mask, keys)
            if filtered:
                out = filtered + '\n' # filtering values
        return out
    
    def get_agent_name(self, metric, param):
        '''Resolve metric name'''
        depend = {
            'CPU': {
                'idle': 'cpu-stat',
                'user': 'cpu-stat',
                'system': 'cpu-stat',
                'iowait': 'cpu-stat',
                'nice': 'cpu-stat'
            },
            'System': {
                'la1': 'cpu-la',
                'la5': 'cpu-la',
                'la15': 'cpu-la',
                'csw': 'cpu-stat',
                'int': 'cpu-stat',
                'numproc': 'cpu-stat',
                'numthreads': 'cpu-stat',
            },
            'Memory': {
                'free': 'mem',
                'used': 'mem',
                'cached': 'mem',
                'buff': 'mem',
            },
            'Disk': {
                'read': 'disk',
                'write': 'disk',
            },
            'Net': {
                'recv': 'net',
                'send': 'net',
                'tx': 'net-tx-rx',
                'rx': 'net-tx-rx',
                'retransmit': 'net-retrans',
                'estab': 'net-tcp',
                'closewait': 'net-tcp',
                'timewait': 'net-tcp',
            }
        }
        if depend[metric][param]:
            return depend[metric][param]
        else:
            return ''

            
class MonitoringDataListener:
    ''' Parent class for data listeners '''
    def monitoring_data(self, data_string):
        ''' Notification about new monitoring data lines '''
        raise RuntimeError("Abstract method needs to be overridden")


class StdOutPrintMon(MonitoringDataListener):
    ''' Simple listener, writing data to stdout '''
    
    def monitoring_data(self, data_string):
            sys.stdout.write(data_string)
