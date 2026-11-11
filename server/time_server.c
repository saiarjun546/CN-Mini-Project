/*
 * time_server.c -- UDP time server for four-timestamp (NTP-style) synchronization.
 *
 * Intended location in the repository: server/time_server.c
 *
 * Build:   gcc -O2 -Wall -Wextra -o time_server time_server.c
 * Run:     ./time_server --host 0.0.0.0 --port 5005 [--verbose]
 *
 * ---------------------------------------------------------------------------
 * WIRE PROTOCOL (version 1)  -- shared with client/time_client.py
 * ---------------------------------------------------------------------------
 * All multi-byte fields are in NETWORK BYTE ORDER (big endian).
 * Doubles are IEEE-754 binary64 values whose 8 bytes are sent big endian.
 * Timestamps are Unix-epoch seconds (with fractional part) as doubles.
 *
 * Common header (12 bytes):
 *   offset size field
 *   0      2    magic       0x5453 ("TS")
 *   2      1    version     1
 *   3      1    msg_type    1 = REQUEST, 2 = RESPONSE
 *   4      8    request_id  uint64, chosen by the client, echoed by the server
 *
 * REQUEST  (20 bytes) = header + T1 (double, client send time)
 * RESPONSE (28 bytes) = header + T2 (double, server receive time)
 *                              + T3 (double, server transmit time)
 *
 * Python struct equivalents:  header "!HBBQ", request "!HBBQd", response "!HBBQdd".
 *
 * The server never uses T1 for anything except a sanity check (it must be a
 * finite number); client-supplied timestamps are not trusted.
 *
 * Limitation: the four-timestamp algorithm assumes the forward and return
 * network delays are roughly equal. Asymmetric delay yields an offset error of
 * (forward_delay - return_delay) / 2, which neither side can detect.
 */

#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <math.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/* ----------------------------- protocol constants ------------------------- */
#define PROTO_MAGIC     0x5453u
#define PROTO_VERSION   1u
#define MSG_REQUEST     1u
#define MSG_RESPONSE    2u

#define HEADER_SIZE     12
#define REQUEST_SIZE    (HEADER_SIZE + 8)       /* header + T1        = 20 */
#define RESPONSE_SIZE   (HEADER_SIZE + 16)      /* header + T2 + T3   = 28 */
#define RECV_BUF_SIZE   512                     /* >> REQUEST_SIZE; larger datagrams are rejected */

#define DEFAULT_HOST    "0.0.0.0"
#define DEFAULT_PORT    5005

_Static_assert(sizeof(double) == 8, "protocol requires 8-byte IEEE-754 doubles");

/* ----------------------------- global state ------------------------------- */
static volatile sig_atomic_t g_running = 1;

struct stats {
    unsigned long received;     /* datagrams received            */
    unsigned long served;       /* valid requests answered       */
    unsigned long rejected;     /* malformed / invalid datagrams */
    unsigned long send_errors;  /* sendto() failures             */
};

/* Reasons a request can be rejected. */
enum parse_result {
    PARSE_OK = 0,
    PARSE_BAD_LENGTH,
    PARSE_BAD_MAGIC,
    PARSE_BAD_VERSION,
    PARSE_BAD_TYPE,
    PARSE_BAD_TIMESTAMP
};

static const char *parse_result_str(enum parse_result r)
{
    switch (r) {
    case PARSE_OK:            return "ok";
    case PARSE_BAD_LENGTH:    return "bad length";
    case PARSE_BAD_MAGIC:     return "bad magic";
    case PARSE_BAD_VERSION:   return "unsupported version";
    case PARSE_BAD_TYPE:      return "bad message type";
    case PARSE_BAD_TIMESTAMP: return "non-finite timestamp";
    }
    return "unknown";
}

/* ----------------------------- signal handling ---------------------------- */
static void handle_signal(int signo)
{
    (void)signo;
    g_running = 0;     /* async-signal-safe: only sets a flag */
}

static int install_signal_handlers(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = handle_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;   /* no SA_RESTART: recvfrom() must return EINTR so we can exit */

    if (sigaction(SIGINT, &sa, NULL) < 0 || sigaction(SIGTERM, &sa, NULL) < 0)
        return -1;
    return 0;
}

/* ----------------------------- byte-order helpers ------------------------- */
static void put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)(v & 0xFF);
}

static uint16_t get_u16(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}

static void put_u64(uint8_t *p, uint64_t v)
{
    for (int i = 7; i >= 0; i--) {
        p[i] = (uint8_t)(v & 0xFF);
        v >>= 8;
    }
}

static uint64_t get_u64(const uint8_t *p)
{
    uint64_t v = 0;
    for (int i = 0; i < 8; i++)
        v = (v << 8) | p[i];
    return v;
}

static void put_double(uint8_t *p, double d)
{
    uint64_t bits;
    memcpy(&bits, &d, sizeof bits);
    put_u64(p, bits);
}

static double get_double(const uint8_t *p)
{
    uint64_t bits = get_u64(p);
    double d;
    memcpy(&d, &bits, sizeof d);
    return d;
}

/* ----------------------------- time helper -------------------------------- */
/* Current Unix-epoch time in seconds (double). Returns 0 on success. */
static int now_seconds(double *out)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_REALTIME, &ts) != 0)
        return -1;
    *out = (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
    return 0;
}

/* ----------------------------- protocol ----------------------------------- */
/* Validate a received datagram as a REQUEST and extract its fields. */
static enum parse_result parse_request(const uint8_t *buf, size_t len,
                                       uint64_t *request_id, double *t1)
{
    if (len != REQUEST_SIZE)
        return PARSE_BAD_LENGTH;
    if (get_u16(buf) != PROTO_MAGIC)
        return PARSE_BAD_MAGIC;
    if (buf[2] != PROTO_VERSION)
        return PARSE_BAD_VERSION;
    if (buf[3] != MSG_REQUEST)
        return PARSE_BAD_TYPE;

    *request_id = get_u64(buf + 4);
    *t1 = get_double(buf + HEADER_SIZE);
    if (!isfinite(*t1))
        return PARSE_BAD_TIMESTAMP;
    return PARSE_OK;
}

/* Fill the constant part of a RESPONSE (header + T2); T3 is written later. */
static void build_response_prefix(uint8_t *out, uint64_t request_id, double t2)
{
    put_u16(out, PROTO_MAGIC);
    out[2] = PROTO_VERSION;
    out[3] = MSG_RESPONSE;
    put_u64(out + 4, request_id);
    put_double(out + HEADER_SIZE, t2);
}

/* ----------------------------- command line ------------------------------- */
static void usage(const char *prog)
{
    fprintf(stderr,
            "Usage: %s [--host ADDR] [--port PORT] [--verbose]\n"
            "\n"
            "UDP time server: answers synchronization requests with the server\n"
            "receive time (T2) and transmit time (T3), both Unix-epoch seconds.\n"
            "\n"
            "Options:\n"
            "  --host ADDR   IPv4 address to bind (default %s)\n"
            "  --port PORT   UDP port to bind, 1-65535 (default %d)\n"
            "  --verbose     log every request and every rejected packet\n"
            "  --help        show this help\n",
            prog, DEFAULT_HOST, DEFAULT_PORT);
}

/* Parse a port number; returns 0 on success. */
static int parse_port(const char *s, int *port)
{
    char *end = NULL;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0' || v < 1 || v > 65535)
        return -1;
    *port = (int)v;
    return 0;
}

/* Returns the value of an option given as "--opt value" or "--opt=value". */
static const char *option_value(int argc, char **argv, int *i, const char *name)
{
    size_t n = strlen(name);
    if (strcmp(argv[*i], name) == 0) {
        if (*i + 1 >= argc) {
            fprintf(stderr, "error: option %s requires a value\n", name);
            return NULL;
        }
        return argv[++(*i)];
    }
    if (strncmp(argv[*i], name, n) == 0 && argv[*i][n] == '=')
        return argv[*i] + n + 1;
    return NULL;
}

/* Returns 0 on success, 1 if --help was shown, -1 on error. */
static int parse_args(int argc, char **argv, const char **host, int *port, int *verbose)
{
    *host = DEFAULT_HOST;
    *port = DEFAULT_PORT;
    *verbose = 0;

    for (int i = 1; i < argc; i++) {
        const char *val;
        if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage(argv[0]);
            return 1;
        } else if (strcmp(argv[i], "--verbose") == 0 || strcmp(argv[i], "-v") == 0) {
            *verbose = 1;
        } else if (strncmp(argv[i], "--host", 6) == 0 &&
                   (argv[i][6] == '\0' || argv[i][6] == '=')) {
            if ((val = option_value(argc, argv, &i, "--host")) == NULL)
                return -1;
            *host = val;
        } else if (strncmp(argv[i], "--port", 6) == 0 &&
                   (argv[i][6] == '\0' || argv[i][6] == '=')) {
            if ((val = option_value(argc, argv, &i, "--port")) == NULL)
                return -1;
            if (parse_port(val, port) != 0) {
                fprintf(stderr, "error: invalid port '%s' (expected 1-65535)\n", val);
                return -1;
            }
        } else {
            fprintf(stderr, "error: unknown argument '%s'\n", argv[i]);
            usage(argv[0]);
            return -1;
        }
    }
    return 0;
}

/* ----------------------------- socket setup ------------------------------- */
/* Create and bind the UDP socket. Returns fd or -1. */
static int create_server_socket(const char *host, int port)
{
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
        fprintf(stderr, "error: '%s' is not a valid IPv4 address\n", host);
        return -1;
    }

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        perror("socket");
        return -1;
    }

    int one = 1;
    if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one) < 0)
        perror("warning: setsockopt(SO_REUSEADDR)");

    /* Wake up once per second so the loop can notice a shutdown request even
     * if a signal arrives just before recvfrom() blocks. */
    struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv) < 0)
        perror("warning: setsockopt(SO_RCVTIMEO)");

    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        fprintf(stderr, "error: cannot bind %s:%d: %s\n", host, port, strerror(errno));
        close(fd);
        return -1;
    }
    return fd;
}

/* ----------------------------- main loop ---------------------------------- */
static void serve(int fd, int verbose, struct stats *st)
{
    uint8_t buf[RECV_BUF_SIZE];
    uint8_t resp[RESPONSE_SIZE];

    while (g_running) {
        struct sockaddr_in cli;
        socklen_t clen = sizeof cli;

        ssize_t n = recvfrom(fd, buf, sizeof buf, 0, (struct sockaddr *)&cli, &clen);

        /* T2: taken as early as possible after the datagram arrives. It is
         * only used if the packet turns out to be a valid request. */
        double t2;
        int t2_ok = (now_seconds(&t2) == 0);

        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
                continue;               /* signal or periodic wake-up */
            perror("recvfrom");
            struct timespec pause = { .tv_sec = 0, .tv_nsec = 10 * 1000 * 1000 };
            nanosleep(&pause, NULL);    /* avoid a busy loop on persistent errors */
            continue;
        }
        st->received++;

        char cli_ip[INET_ADDRSTRLEN] = "?";
        inet_ntop(AF_INET, &cli.sin_addr, cli_ip, sizeof cli_ip);

        uint64_t request_id = 0;
        double t1 = 0.0;
        enum parse_result pr = parse_request(buf, (size_t)n, &request_id, &t1);
        if (pr != PARSE_OK || !t2_ok) {
            st->rejected++;
            if (verbose)
                fprintf(stderr, "rejected %zd-byte packet from %s:%u (%s)\n", n, cli_ip,
                        (unsigned)ntohs(cli.sin_port),
                        t2_ok ? parse_result_str(pr) : "clock read failure");
            continue;
        }

        /* Build everything except T3 first, then read the clock as late as
         * possible so T3 is as close as possible to the actual transmission. */
        build_response_prefix(resp, request_id, t2);
        double t3;
        if (now_seconds(&t3) != 0) {
            st->rejected++;
            fprintf(stderr, "error: clock_gettime failed; dropping request %llu\n",
                    (unsigned long long)request_id);
            continue;
        }
        put_double(resp + HEADER_SIZE + 8, t3);

        ssize_t sent = sendto(fd, resp, sizeof resp, 0, (struct sockaddr *)&cli, clen);
        if (sent != (ssize_t)sizeof resp) {
            st->send_errors++;
            fprintf(stderr, "sendto %s:%u failed: %s\n", cli_ip,
                    (unsigned)ntohs(cli.sin_port),
                    sent < 0 ? strerror(errno) : "short send");
            continue;
        }

        st->served++;
        if (verbose)
            printf("served id=%llu client=%s:%u T1=%.6f T2=%.6f T3=%.6f\n",
                   (unsigned long long)request_id, cli_ip, (unsigned)ntohs(cli.sin_port),
                   t1, t2, t3);
    }
}

int main(int argc, char **argv)
{
    const char *host;
    int port, verbose;

    int rc = parse_args(argc, argv, &host, &port, &verbose);
    if (rc != 0)
        return rc > 0 ? 0 : 2;

    setvbuf(stdout, NULL, _IOLBF, 0);   /* line-buffered: logs visible when redirected */

    if (install_signal_handlers() < 0) {
        perror("sigaction");
        return 1;
    }

    int fd = create_server_socket(host, port);
    if (fd < 0)
        return 1;

    printf("UDP time server listening on %s:%d (protocol v%u)\n", host, port, PROTO_VERSION);

    struct stats st = {0, 0, 0, 0};
    serve(fd, verbose, &st);

    close(fd);
    printf("Shutting down: received=%lu served=%lu rejected=%lu send_errors=%lu\n",
           st.received, st.served, st.rejected, st.send_errors);
    return 0;
}
