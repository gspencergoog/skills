class BrokenCpp {
public:
  int bad_sig(int x, {
    return x;
  }
};

int recovered_cpp_fn() {
  return 7;
}
