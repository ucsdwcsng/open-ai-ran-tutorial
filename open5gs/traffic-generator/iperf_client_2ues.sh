echo "iperf -c 10.45.1.2 -u -i 1 -b $1 -t $3"
iperf -c 10.45.1.2 -u -i 1 -b $1 -t $3  &


sleep 3

echo "iperf -c 10.45.1.3 -u -i 1 -b $2 -t $3"
iperf -c 10.45.1.3 -u -i 1 -b $2 -t $3 
