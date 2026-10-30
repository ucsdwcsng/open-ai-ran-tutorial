// SPDX-License-Identifier: GPL-3.0
//
// zmq_broker: one OCUDU gNB <-> N OAI (Duranta) UEs over ZMQ, single-threaded.
//
// Drop-in replacement for ../multi_ue_broker.py (same options). The Python/GNU Radio broker runs
// every block in its own thread (~110 threads at 4 UEs) with a buffer copy between each; in a
// lock-step REQ/REP chain that adds latency to every round trip, and the round-trip time is what
// bounds how close to real time the chain runs. This does the same work in one zmq_poll loop.
//
// Wire protocol (identical to gr-zeromq req_source / rep_sink as the peers use it): a REQ side sends
// a request (the peers send 1 byte; content ignored), the REP side answers with one message of
// complex float32 samples.
//
//   downlink:  gNB tx (REP) <-REQ-- broker --> per-UE DL queue (x DL gain) --REP--> UE i rx (REQ)
//   uplink:    UE i tx (REP) <-REQ-- broker --> per-UE UL queue (x UL gain) -> sum [+ gNB noise]
//                                                                     --REP--> gNB rx (REQ)
//
// Pacing: only the DL is paced (the loop is closed: UEs transmit only once they have received DL).
// Samples are pulled from the gNB no faster than --srate, with a bounded credit (--credit-ms) so a
// stall or the start-up wait cannot be paid back later as a faster-than-real-time burst.
//
// Build: g++ -O3 -march=native -std=c++17 zmq_broker.cpp -lzmq -o zmq_broker

#include <zmq.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <complex>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <string>
#include <vector>

using cf    = std::complex<float>;
using clk   = std::chrono::steady_clock;
static volatile std::sig_atomic_t g_stop = 0;

// FIFO of samples: a vector with a read offset, compacted when the consumed part dominates.
struct fifo {
  std::vector<cf> buf;
  size_t          head = 0;
  size_t          size() const { return buf.size() - head; }
  const cf*       data() const { return buf.data() + head; }
  void            push(const cf* p, size_t n, float gain)
  {
    size_t old = buf.size();
    buf.resize(old + n);
    if (gain == 1.0f) {
      std::memcpy(buf.data() + old, p, n * sizeof(cf));
    } else {
      for (size_t k = 0; k != n; ++k) {
        buf[old + k] = p[k] * gain;
      }
    }
  }
  void pop(size_t n)
  {
    head += n;
    if (head == buf.size()) {
      buf.clear();
      head = 0;
    } else if (head > (1u << 16) && head * 2 > buf.size()) {
      buf.erase(buf.begin(), buf.begin() + head);
      head = 0;
    }
  }
};

struct args_t {
  int                num_ues   = 0;
  std::string        gnb_tx    = "tcp://127.0.0.1:2101";
  std::string        gnb_rx    = "tcp://127.0.0.1:2100";
  std::string        ue_tx     = "tcp://10.201.{i}.2:2001";
  std::string        ue_rx     = "tcp://10.201.{i}.1:2000";
  double             srate     = 23.04e6;
  std::vector<float> dl_gain_db{0.0f}, ul_gain_db{0.0f};
  bool               throttle  = true;
  bool               has_noise = false;
  float              ul_noise_dbfs = 0;
  unsigned           seed          = 0;
  double             credit_ms     = 5.0;
  double             ue_timeout_ms = 200; // a UE silent this long is dropped (0 = never)
  size_t             chunk         = 11520; // max samples per reply (one 30 kHz slot at 23.04 MS/s)
  size_t             queue         = 23040;  // pull from the gNB while some UE's DL queue holds less
  size_t             max_queue     = 460800; // hard cap per queue (20 ms): back-pressure only beyond it
};

static std::string fmt_ep(const std::string& f, int i)
{
  std::string s   = f;
  size_t      pos = s.find("{i}");
  if (pos != std::string::npos) {
    s.replace(pos, 3, std::to_string(i));
  }
  return s;
}

static std::vector<float> expand(const std::vector<float>& v, int n, const char* name)
{
  if (v.size() == 1) {
    return std::vector<float>(n, v[0]);
  }
  if ((int)v.size() != n) {
    std::fprintf(stderr, "--%s takes 1 value or %d values (one per UE)\n", name, n);
    std::exit(1);
  }
  return v;
}

static void usage()
{
  std::puts("zmq_broker -n N [--gnb-tx EP] [--gnb-rx EP] [--ue-tx FMT] [--ue-rx FMT] [--srate S]\n"
            "           [--dl-gain-db G..] [--ul-gain-db G..] [--ul-noise-dbfs D] [--seed S]\n"
            "           [--no-throttle] [--credit-ms MS] [--chunk N] [--queue N] [--max-queue N]\n"
            "           [--ue-timeout-ms MS]\n"
            "  FMT: endpoint with {i} = UE index 1..N. Same options as multi_ue_broker.py; its\n"
            "  --throttle-rearm-s / --ul-throttle / --timeout-ms / --hwm are accepted and ignored.");
}

static args_t parse(int argc, char** argv)
{
  args_t a;
  auto floats = [&](int& k) {
    std::vector<float> v;
    while (k + 1 < argc) {
      char* end = nullptr;
      float f   = std::strtof(argv[k + 1], &end);
      if (end == argv[k + 1] || *end != '\0') {
        break;
      }
      v.push_back(f);
      ++k;
    }
    return v;
  };
  for (int k = 1; k < argc; ++k) {
    std::string o = argv[k];
    auto        next = [&]() -> const char* {
      if (k + 1 >= argc) {
        usage();
        std::exit(1);
      }
      return argv[++k];
    };
    if (o == "-n" || o == "--num-ues") a.num_ues = std::atoi(next());
    else if (o == "--gnb-tx") a.gnb_tx = next();
    else if (o == "--gnb-rx") a.gnb_rx = next();
    else if (o == "--ue-tx") a.ue_tx = next();
    else if (o == "--ue-rx") a.ue_rx = next();
    else if (o == "--srate") a.srate = std::atof(next());
    else if (o == "--dl-gain-db") a.dl_gain_db = floats(k);
    else if (o == "--ul-gain-db") a.ul_gain_db = floats(k);
    else if (o == "--ul-noise-dbfs") { a.has_noise = true; a.ul_noise_dbfs = std::atof(next()); }
    else if (o == "--seed") a.seed = std::atoi(next());
    else if (o == "--throttle") a.throttle = true;
    else if (o == "--no-throttle") a.throttle = false;
    else if (o == "--credit-ms") a.credit_ms = std::atof(next());
    else if (o == "--ue-timeout-ms") a.ue_timeout_ms = std::atof(next());
    else if (o == "--chunk") a.chunk = std::atol(next());
    else if (o == "--queue") a.queue = std::atol(next());
    else if (o == "--max-queue") a.max_queue = std::atol(next());
    else if (o == "--throttle-rearm-s" || o == "--timeout-ms" || o == "--hwm") next();
    else if (o == "--ul-throttle" || o == "--no-ul-throttle") {}
    else if (o == "-h" || o == "--help") { usage(); std::exit(0); }
    else { std::fprintf(stderr, "unknown option %s\n", o.c_str()); usage(); std::exit(1); }
  }
  if (a.num_ues < 1) {
    usage();
    std::exit(1);
  }
  a.dl_gain_db = expand(a.dl_gain_db, a.num_ues, "dl-gain-db");
  a.ul_gain_db = expand(a.ul_gain_db, a.num_ues, "ul-gain-db");
  return a;
}

static void* sock(void* ctx, int type, const std::string& ep, bool bind)
{
  void* s    = zmq_socket(ctx, type);
  int   zero = 0, one = 1;
  zmq_setsockopt(s, ZMQ_LINGER, &zero, sizeof(zero));
  if (type == ZMQ_REQ) {
    // A peer that dies leaves a REQ socket waiting for a reply forever; relaxed + correlate lets it
    // send a fresh request, so a restarted UE can be picked up again.
    zmq_setsockopt(s, ZMQ_REQ_RELAXED, &one, sizeof(one));
    zmq_setsockopt(s, ZMQ_REQ_CORRELATE, &one, sizeof(one));
  }
  int rc = bind ? zmq_bind(s, ep.c_str()) : zmq_connect(s, ep.c_str());
  if (rc != 0) {
    std::fprintf(stderr, "[broker] %s %s failed: %s\n", bind ? "bind" : "connect", ep.c_str(), zmq_strerror(errno));
    std::exit(1);
  }
  return s;
}

static bool send_samples(void* s, fifo& q, size_t max_n)
{
  size_t n = std::min(q.size(), max_n);
  if (zmq_send(s, q.data(), n * sizeof(cf), 0) < 0) {
    std::fprintf(stderr, "[broker] send failed: %s\n", zmq_strerror(errno));
    return false;
  }
  q.pop(n);
  return true;
}

int main(int argc, char** argv)
{
  args_t     a = parse(argc, argv);
  const int  n = a.num_ues;
  std::signal(SIGINT, [](int) { g_stop = 1; });
  std::signal(SIGTERM, [](int) { g_stop = 1; });

  void* ctx = zmq_ctx_new();
  zmq_ctx_set(ctx, ZMQ_IO_THREADS, 2);
  void*              gnb_tx = sock(ctx, ZMQ_REQ, a.gnb_tx, false);
  void*              gnb_rx = sock(ctx, ZMQ_REP, a.gnb_rx, true);
  std::vector<void*> ue_tx(n), ue_rx(n);
  std::vector<float> dl_gain(n), ul_gain(n);
  for (int i = 0; i != n; ++i) {
    ue_rx[i]   = sock(ctx, ZMQ_REP, fmt_ep(a.ue_rx, i + 1), true);
    ue_tx[i]   = sock(ctx, ZMQ_REQ, fmt_ep(a.ue_tx, i + 1), false);
    dl_gain[i] = std::pow(10.0f, a.dl_gain_db[i] / 20.0f);
    ul_gain[i] = std::pow(10.0f, a.ul_gain_db[i] / 20.0f);
  }

  // gNB receiver noise: a precomputed table walked from a random offset per block (as fastnoise_source).
  std::vector<cf> noise;
  std::mt19937    rng(a.seed ? a.seed : std::random_device{}());
  if (a.has_noise) {
    float                           amp = std::pow(10.0f, a.ul_noise_dbfs / 20.0f) / std::sqrt(2.0f);
    std::normal_distribution<float> g(0.0f, amp);
    noise.resize(1u << 20);
    for (auto& v : noise) {
      v = cf(g(rng), g(rng));
    }
  }

  std::printf("[broker] C++ single-thread, gNB tx %s -> %d UE(s) -> gNB rx %s\n", a.gnb_tx.c_str(), n, a.gnb_rx.c_str());
  if (a.has_noise) {
    std::printf("[broker] gNB receiver noise %.1f dBFS\n", a.ul_noise_dbfs);
  }
  for (int i = 0; i != n; ++i) {
    std::printf("[broker]   UE%d: rx %s  tx %s  (DL %+.1f dB, UL %+.1f dB)\n", i + 1, fmt_ep(a.ue_rx, i + 1).c_str(),
                fmt_ep(a.ue_tx, i + 1).c_str(), a.dl_gain_db[i], a.ul_gain_db[i]);
  }
  std::printf("[broker] running (chunk %zu, queue %zu, pacing %s)\n", a.chunk, a.queue,
              a.throttle ? "DL at srate" : "off");
  std::fflush(stdout);

  std::vector<fifo> dl_q(n), ul_q(n);
  fifo              ul_out;
  std::vector<cf>   tmp;
  bool              gnb_tx_waiting = false, gnb_rx_pending = false;
  std::vector<char> ue_tx_waiting(n, 0), ue_rx_pending(n, 0);
  // Liveness. The UL sum needs every UE, so one UE that stops (e.g. an OAI assert) would freeze the
  // whole cell. Once every UE is streaming: if the sum has been blocked for ue_timeout_ms while some
  // UEs have UL waiting and others have none, the ones with none are dropped for good (left out of
  // the sum and of DL flow control) and the others carry on. Plain silence cannot tell them apart:
  // once the chain stalls, every UE goes quiet, waiting for DL.
  // It cannot be taken back: UL/DL timing in ZMQ emulation is pure sample counting from a common
  // start, so a UE joining mid-stream would transmit at an arbitrary offset and never attach. The
  // same is why start-up stays frozen until every UE has delivered UL.
  std::vector<char>             alive(n, 1), seen_ul(n, 0);
  std::vector<clk::time_point>  last_seen(n, clk::now()), last_req(n, clk::now());
  bool                          all_up = false, starved = false;
  clk::time_point               starved_since;
  const char        req = 0;

  // DL pacing: samples may be pulled from the gNB up to (now - t0) * srate, with at most credit_ms
  // of unused allowance carried forward.
  auto     t0       = clk::now();
  uint64_t dl_count = 0;
  bool     started  = false;
  double   credit   = a.credit_ms * 1e-3 * a.srate;

  // Real-time report every 5 s: samples actually moved per second, vs --srate (100% = real time).
  auto     rep_t    = clk::now();
  uint64_t rep_dl   = 0, rep_ul = 0, ul_count = 0;

  std::vector<zmq_pollitem_t> items(2 + 2 * n);
  std::vector<char>           msgbuf;

  while (!g_stop) {
    // 1. Issue requests to REP peers that have room.
    // Flow control. Pull from the gNB while any UE's DL queue is short, and keep asking every UE for
    // UL, with back-pressure only at a generous hard cap. Tight limits deadlock the closed loop: a UE
    // that is not asked for UL stops reading DL, its full DL queue then blocks the gNB for everyone,
    // and the other UEs never get the DL they need to produce their UL.
    size_t dl_min = SIZE_MAX, dl_max = 0;
    for (int i = 0; i != n; ++i) {
      if (alive[i]) {
        dl_min = std::min(dl_min, dl_q[i].size());
        dl_max = std::max(dl_max, dl_q[i].size());
      }
    }
    const bool dl_room = dl_min == SIZE_MAX || (dl_min < a.queue && dl_max < a.max_queue);
    bool dl_allowed = true;
    if (a.throttle && started) {
      double allowed = std::chrono::duration<double>(clk::now() - t0).count() * a.srate;
      if (allowed - (double)dl_count > credit) {
        t0      = clk::now() - std::chrono::duration_cast<clk::duration>(
                                  std::chrono::duration<double>(((double)dl_count + credit) / a.srate));
        allowed = (double)dl_count + credit;
      }
      dl_allowed = (double)dl_count < allowed;
    }
    if (!gnb_tx_waiting && dl_room && dl_allowed) {
      if (zmq_send(gnb_tx, &req, 1, 0) == 1) {
        gnb_tx_waiting = true;
      }
    }
    for (int i = 0; i != n; ++i) {
      if (!alive[i]) {
        continue;
      }
      if (!ue_tx_waiting[i] && ul_q[i].size() < a.max_queue) {
        if (zmq_send(ue_tx[i], &req, 1, 0) == 1) {
          ue_tx_waiting[i] = 1;
          last_req[i]      = clk::now();
        }
      }
    }

    // 2. Wait for any socket. Short timeout while the DL is held back by pacing, so it resumes on time.
    items[0] = {gnb_tx, 0, ZMQ_POLLIN, 0};
    items[1] = {gnb_rx, 0, ZMQ_POLLIN, 0};
    for (int i = 0; i != n; ++i) {
      items[2 + i]     = {ue_tx[i], 0, ZMQ_POLLIN, 0};
      items[2 + n + i] = {ue_rx[i], 0, ZMQ_POLLIN, 0};
    }
    bool held = !gnb_tx_waiting && dl_room && !dl_allowed;
    int  rc   = zmq_poll(items.data(), items.size(), held ? 0 : 5);
    if (rc < 0) {
      if (errno == EINTR) {
        continue;
      }
      std::fprintf(stderr, "[broker] poll: %s\n", zmq_strerror(errno));
      break;
    }
    if (held && rc == 0) {
      struct timespec ts = {0, 20000};
      nanosleep(&ts, nullptr);
    }

    // 3. Receive.
    auto recv_into = [&](void* s) -> std::pair<const cf*, size_t> {
      zmq_msg_t m;
      zmq_msg_init(&m);
      int nb = zmq_msg_recv(&m, s, 0);
      if (nb < 0) {
        zmq_msg_close(&m);
        return {nullptr, 0};
      }
      msgbuf.assign((char*)zmq_msg_data(&m), (char*)zmq_msg_data(&m) + nb);
      zmq_msg_close(&m);
      return {reinterpret_cast<const cf*>(msgbuf.data()), (size_t)nb / sizeof(cf)};
    };
    if (items[0].revents & ZMQ_POLLIN) {
      auto [p, k] = recv_into(gnb_tx);
      for (int i = 0; i != n; ++i) {
        if (alive[i]) {
          dl_q[i].push(p, k, dl_gain[i]);
        }
      }
      dl_count += k;
      gnb_tx_waiting = false;
      if (!started && k) {
        started = true; // pacing clock starts with the first samples, not with the broker
        t0      = clk::now();
        dl_count = k;
      }
    }
    if (items[1].revents & ZMQ_POLLIN) {
      recv_into(gnb_rx);
      gnb_rx_pending = true;
    }
    for (int i = 0; i != n; ++i) {
      if (items[2 + i].revents & ZMQ_POLLIN) {
        auto [p, k] = recv_into(ue_tx[i]);
        ul_q[i].push(p, k, ul_gain[i]);
        ue_tx_waiting[i] = 0;
        last_seen[i]     = clk::now();
        seen_ul[i]       = 1;
      }
      if (items[2 + n + i].revents & ZMQ_POLLIN) {
        recv_into(ue_rx[i]);
        ue_rx_pending[i] = 1;
        last_seen[i]     = clk::now();
      }
    }
    if (!all_up && std::all_of(seen_ul.begin(), seen_ul.end(), [](char c) { return c != 0; })) {
      all_up = true;
      std::fill(last_seen.begin(), last_seen.end(), clk::now());
      std::printf("[broker] all %d UEs streaming\n", n);
      std::fflush(stdout);
    }
    if (all_up && a.ue_timeout_ms > 0) {
      bool some_have = false, some_empty = false;
      for (int i = 0; i != n; ++i) {
        if (alive[i]) {
          (ul_q[i].size() ? some_have : some_empty) = true;
        }
      }
      if (some_have && some_empty) {
        if (!starved) {
          starved       = true;
          starved_since = clk::now();
        } else if (std::chrono::duration<double, std::milli>(clk::now() - starved_since).count() > a.ue_timeout_ms) {
          for (int i = 0; i != n; ++i) {
            if (alive[i] && ul_q[i].size() == 0) {
              alive[i] = 0;
              dl_q[i]  = fifo();
              std::printf("[broker] UE%d sent no UL for %.0f ms while the others had: dropped for good, the "
                          "other UEs carry on\n",
                          i + 1, a.ue_timeout_ms);
            }
          }
          std::fflush(stdout);
          starved = false;
        }
      } else {
        starved = false;
      }
    }

    // 4. UL: sum what every UE has delivered (+ gNB noise).
    size_t k = SIZE_MAX;
    for (int i = 0; i != n; ++i) {
      if (alive[i]) {
        k = std::min(k, ul_q[i].size());
      }
    }
    if (k == SIZE_MAX) {
      // No UE left: keep the gNB fed with its own noise floor (silence) at the rate it asks for.
      k = (gnb_rx_pending && ul_out.size() == 0) ? a.chunk : 0;
    }
    if (k) {
      tmp.assign(k, cf(0, 0));
      for (int i = 0; i != n; ++i) {
        if (!alive[i]) {
          continue;
        }
        const cf* p = ul_q[i].data();
        for (size_t j = 0; j != k; ++j) {
          tmp[j] += p[j];
        }
      }
      if (!noise.empty()) {
        size_t off = rng() & (noise.size() - 1);
        for (size_t j = 0; j != k; ++j) {
          tmp[j] += noise[(off + j) & (noise.size() - 1)];
        }
      }
      ul_out.push(tmp.data(), k, 1.0f);
      for (int i = 0; i != n; ++i) {
        if (alive[i]) {
          ul_q[i].pop(k);
        }
      }
    }

    // 5. Answer pending requests.
    for (int i = 0; i != n; ++i) {
      if (ue_rx_pending[i] && dl_q[i].size()) {
        if (send_samples(ue_rx[i], dl_q[i], a.chunk)) {
          ue_rx_pending[i] = 0;
        }
      }
    }
    if (gnb_rx_pending && ul_out.size()) {
      size_t before = ul_out.size();
      if (send_samples(gnb_rx, ul_out, a.chunk)) {
        gnb_rx_pending = false;
        ul_count += before - ul_out.size();
      }
    }

    double dt = std::chrono::duration<double>(clk::now() - rep_t).count();
    if (dt >= 5.0) {
      double dl = (dl_count - rep_dl) / dt / 1e6, ul = (ul_count - rep_ul) / dt / 1e6;
      std::printf("[broker] rate DL %.2f MS/s (%.1f%%)  UL %.2f MS/s (%.1f%%)\n", dl, 100 * dl * 1e6 / a.srate, ul,
                  100 * ul * 1e6 / a.srate);
      std::fflush(stdout);
      rep_t  = clk::now();
      rep_dl = dl_count;
      rep_ul = ul_count;
    }
  }

  std::puts("[broker] stopping");
  // LINGER 0: closing does not block on peers that are gone.
  zmq_close(gnb_tx);
  zmq_close(gnb_rx);
  for (int i = 0; i != n; ++i) {
    zmq_close(ue_tx[i]);
    zmq_close(ue_rx[i]);
  }
  zmq_ctx_term(ctx);
  return 0;
}
