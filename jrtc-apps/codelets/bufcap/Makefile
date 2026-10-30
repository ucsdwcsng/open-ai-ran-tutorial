# bufcap: one source, one object per cap. Normally driven by buffer-control-experiments/bufcap.sh:
#     bufcap.sh build 16k 64k 256k 1m off
# which runs, per cap:   make one NAME=<name> CAP=<bytes>   (CAP=0 -> bufcap_off.o)

PROTO_AND_SCHEMA :=

include ../Makefile.defs

NAME ?= 64k
CAP  ?= 65536

one:
	@echo "--------- bufcap.cpp -> bufcap_$(NAME).o (CAP_BYTES=$(CAP)) ---------"
	@$(CXX) $(CXXFLAGS) -DCAP_BYTES=$(CAP) $(INC) -c bufcap.cpp -o bufcap_$(NAME).o
	@$(VERIFIER_BIN) bufcap_$(NAME).o >/dev/null 2>&1 \
		&& echo "verifier: OK" \
		|| { echo "verifier: FAILED -- the gNB would reject bufcap_$(NAME).o"; rm -f bufcap_$(NAME).o; exit 1; }

# dynamic controller: observer (mac_sched_harq_dl) + controller (rlc_dl_ctrl), built together
W ?= 50
RTT ?= 30
HOLD ?= 60
RAMP ?= 200
dyn:
	@echo "--------- bufdyn_obs.o + bufdyn_ctl_$(NAME).o (W=$(W) RTT0=$(RTT) HOLD=$(HOLD) RAMP=$(RAMP) ms) ---------"
	@$(CXX) $(CXXFLAGS) $(INC) -c bufdyn_obs.cpp -o bufdyn_obs.o
	@$(CXX) $(CXXFLAGS) -DW_TARGET_MS=$(W) -DRTT0_MS=$(RTT) -DHOLD_MS=$(HOLD) -DRAMP_MS=$(RAMP) $(INC) -c bufdyn_ctl.cpp -o bufdyn_ctl_$(NAME).o
	@for o in bufdyn_obs.o bufdyn_ctl_$(NAME).o; do \
		$(VERIFIER_BIN) $$o >/dev/null 2>&1 && echo "verifier: OK ($$o)" \
		|| { echo "verifier: FAILED -- $$o"; rm -f $$o; exit 1; }; done

clean:
	rm -f *.o

cleanall: clean

.PHONY: one dyn clean cleanall
