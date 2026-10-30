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

clean:
	rm -f *.o

cleanall: clean

.PHONY: one clean cleanall
