from flask import Flask, Response
import mss
import cv2
import logging

app = Flask(__name__)
logging.getLogger('waitress').setLevel(logging.ERROR)
def generate_frames():
    with mss.mss() as sct:
        monitor = sct.monitors[0]  # We assume there is only one monitor
        while True:
            screenshot = sct.shot(output='fullscreen.png')  # Provide 'output' only once
            frame = cv2.imread('fullscreen.png')
            if frame is not None:
                ret, buffer = cv2.imencode('.jpg', frame)
                if not ret:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

@app.route('/video')
def video():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.debug = True
    # app.run()
    app.run(host='0.0.0.0', port=8000)
