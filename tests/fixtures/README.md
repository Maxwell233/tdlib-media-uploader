# Media regression fixture

`remux_h264_aac.mp4` is an original one-second synthetic blue frame with a 440 Hz
tone (64 × 48, H.264/AAC). It contains no third-party media. Keeping the encoded
sample allows the LGPL release tools to test stream copying without `libx264`.

Generated with FFmpeg 7.1 using:

```sh
ffmpeg -f lavfi -i color=c=blue:s=64x48:r=10 \
  -f lavfi -i sine=frequency=440:sample_rate=44100 -t 1 \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 32k \
  -movflags +faststart remux_h264_aac.mp4
```
