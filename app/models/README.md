# Face model assets

The server uses OpenCV Zoo's YuNet detector and SFace recognizer locally. The models do not send photos to a third-party service.

| File | Purpose | SHA-256 | License |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` | Detects and aligns one face | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` | MIT |
| `face_recognition_sface_2021dec.onnx` | Creates face features and compares identity | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` | Apache 2.0 |

Upstream model documentation and licenses:

- [YuNet model and MIT license](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
- [SFace model and Apache 2.0 license](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface)

The SFace similarity match threshold defaults to `LOCAL_FACE_MATCH_THRESHOLD=0.363`, the threshold documented by OpenCV's reference implementation. A score below the threshold is sent to manual review rather than automatically rejecting attendance. Validate this threshold with representative, consented camp data before relying on automated passes.

These models perform face matching, not liveness detection. Do not describe a face match as proof that a photo came from a live camera. Unclear results and provider errors go to the Camp Director for review.
