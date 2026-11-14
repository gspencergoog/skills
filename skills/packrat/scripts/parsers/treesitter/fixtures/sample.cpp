namespace engine::core {

namespace {
int hidden_seed = 7;
}

/** 2D integer coordinate. */
struct Point {
  int x;
  int y;
private:
  int tag_;
};

/** Pixel channel format. */
enum class PixelFormat {
  Rgba8,
  Float16 = 2,
};

using ByteBuffer = unsigned char*;
typedef int HandleId;

/**
 * Hardware surface wrapper.
 */
class Surface {
  int secret_id_ = 0;

public:
  /** Constructs a surface with dimensions. */
  Surface(int w, int h) : width_(w), height_(h) {}
  ~Surface();

  /// Returns current width.
  int width() const { return width_; }
  void resize(int w, int h);

protected:
  int width_ = 0;
  int height_ = 0;
};

void Surface::resize(int w, int h) {
  auto clamp = [](int v) { return v < 0 ? 0 : v; };
  width_ = clamp(w);
  height_ = clamp(h);
}

/*! Computes total pixel area. */
int compute_area(int w, int h) {
  return w * h;
}

}  // namespace engine::core
