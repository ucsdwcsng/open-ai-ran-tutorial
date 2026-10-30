#!/usr/bin/perl
# Plain UDP receiver for scripts/traffic_nue.sh --plain: binds IP:PORT inside a UE's netns, counts the datagrams that
# arrive and logs one line per second in iperf3's interval format, so `traffic_nue.sh status`, edgeric_eval.sh and
# edgeric-rt/tools/udp_window.py read it like an iperf3 server log. No control connection: a UE starved for seconds
# cannot break the flow, unlike iperf3, whose TCP control channel shares the UE's congested DL queue.
#
#   perl udp_sink.pl <ip> <port> <logfile>
use strict;
use warnings;
use IO::Socket::INET;
use Socket qw(SOL_SOCKET SO_RCVBUF);

# perl-base only (the UE image has no Time::HiRes): whole-second intervals from time(), 4-argument select to wait.
my ($ip, $port, $log) = @ARGV;
die "usage: udp_sink.pl <ip> <port> <logfile>\n" unless defined $log;
my $s = IO::Socket::INET->new(LocalAddr => $ip, LocalPort => $port, Proto => 'udp', ReuseAddr => 1)
  or die "bind $ip:$port: $!\n";
setsockopt($s, SOL_SOCKET, SO_RCVBUF, 8 << 20);
open(my $fh, '>', $log) or die "open $log: $!\n";
$fh->autoflush(1);
my ($bytes, $pkts, $buf) = (0, 0, '');
my $t0 = time;
my $sec = $t0;
my $rin = '';
vec($rin, fileno($s), 1) = 1;
while (1) {
  my $now = time;
  while ($sec < $now) {    # one line per whole second that has passed
    printf $fh "[  1] %7.2f-%-7.2f sec  %10d Bytes  %8.2f Mbits/sec  %6d datagrams\n",
      $sec - $t0, $sec + 1 - $t0, $bytes, $bytes * 8 / 1e6, $pkts;
    ($bytes, $pkts) = (0, 0);
    $sec++;
  }
  next unless select(my $rout = $rin, undef, undef, 0.2) > 0;
  while (defined($s->recv($buf, 65535, 0x40))) {    # MSG_DONTWAIT: drain what is queued
    last unless length $buf;
    $bytes += length $buf;
    $pkts++;
  }
}
