#cd /src
tdnf install -y zeromq-devel pkg-config
rm -rf build
mkdir build
cd build
cmake -DENABLE_JBPF=on   -DCMAKE_C_FLAGS="-I/usr/include/x86_64-linux-gnu"   -DCMAKE_CXX_FLAGS="-I/usr/include/x86_64-linux-gnu" -DENABLE_ZEROMQ=ON -DENABLE_DPDK=True -DINITIALIZE_SUBMODULES=OFF -DCMAKE_C_FLAGS="-Wno-error=unused-variable" ..
make -j8 gnb
cd ..
