import base64
import datetime
import json
import math
import os
import threading
import time
from ast import List
from collections import defaultdict

import cv2
import numpy as np
from flask import Flask, Response, redirect, render_template, request, session
from flask_mail import Mail, Message
from ultralytics import YOLO

from suraksha.services.firebase import auth, db, storage
from suraksha.config import config
from suraksha.services.chat import get_chat_response


classNames = []
thread_objects = []
json_path = os.path.join(os.path.dirname(__file__), "data", "detection_classes.json")
with open(json_path, "r") as f:
    data = json.load(f)
    classNames = data["class_names"]
    thread_objects = data["threat_objects"]

app = Flask(__name__)
app.secret_key = "secret"
app.app_context().push()

app.config["MAIL_SERVER"] = config.MAIL_SERVER
app.config["MAIL_PORT"] = config.MAIL_PORT
app.config["MAIL_USERNAME"] = config.MAIL_USERNAME
app.config["MAIL_PASSWORD"] = config.MAIL_PASSWORD
app.config["MAIL_USE_TLS"] = config.MAIL_USE_TLS
app.config["MAIL_USE_SSL"] = config.MAIL_USE_SSL

model = YOLO("yolov8n.pt")
mail = Mail(app)


@app.route("/chat", methods=["GET", "POST"])
def chat():
    msg = request.form["msg"]
    user_id = session.get('user', {}).get('localId')
    
    if not user_id:
        return "Please log in to use the chat feature."
    
    conversation_history = session.get('conversation_history', [])
    
    response, updated_history = get_chat_response(msg, conversation_history, user_id)
    
    # Store the updated conversation history in the session
    session['conversation_history'] = updated_history
    
    return response


def send_email(msg, subject, sender, recipients):
    msg = Message(subject, sender=sender, recipients=recipients, body=msg)
    mail.send(msg)


def send_email_in_thread(msg, subject, sender, recipients):
    def run_in_context():
        with app.app_context():
            send_email(msg, subject, sender, recipients)

    thread = threading.Thread(target=run_in_context)
    thread.start()


def get_cameras() -> list:
    cameras = []
    index = 0
    while True:
        camera = cv2.VideoCapture(index)
        if not camera.isOpened():
            break
        cameras.append(index)
        camera.release()
        index += 1
    return cameras


def send_analytics(data: dict, userId: str) -> None:
    if len(data) == 0:
        return

    if userId is None or userId == "":
        return

    data_in_millis = round(time.time() * 1000)

    try:
        db.child("analytics").child(userId).child(data_in_millis).set(data)
    except Exception as e:
        print(e)


def upload_frame_to_firebase(frame, user_id, timestamp, folder="records"):

    # Convert the frame to PNG image data
    _, buffer = cv2.imencode(".png", frame)
    image_data = buffer.tobytes()

    # Create a unique filename for the frame
    filename = f"{user_id}/{folder}/{timestamp}.png"

    # Upload the frame to Firebase Storage
    storage.child(filename).put(image_data)


def get_images(user_id):
    images = []
    try:
        storage.child(user_id).get_url(user_id)
    except Exception as e:
        print(e)

    return images


@app.route("/capture", methods=["POST"])
def capture():
    data_url = request.json["img_data"]
    img_data = base64.b64decode(data_url.split(",")[1])
    nparry = np.frombuffer(img_data, np.uint8)
    img = cv2.imdecode(nparry, cv2.IMREAD_COLOR)
    period = datetime.datetime.now()

    upload_frame_to_firebase(
        img,
        session["user"]["localId"],
        period.strftime("%Y-%m-%d %H:%M:%S"),
        folder="captures",
    )

    return "success"


def gen_frames(user_id, user_email):
    camera = cv2.VideoCapture(0)
    next_time = datetime.datetime.now()
    delta = datetime.timedelta(seconds=30)
    objectData = {}  # person -> {freq, maxConfidence, minConfidence}
    email_sent = False

    while True:
        period = datetime.datetime.now()

        success, frame = camera.read()
        frame = cv2.flip(frame, 1)
        results = model(frame, stream=True, verbose=False)

        objectsFreq = defaultdict(List)

        # coordinates
        for r in results:
            boxes = r.boxes

            for box in boxes:
                # bounding box
                x1, y1, x2, y2 = box.xyxy[0]
                x1, y1, x2, y2 = (
                    int(x1),
                    int(y1),
                    int(x2),
                    int(y2),
                )  # convert to int values

                # put box in cam
                cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 255), 3)

                # confidence
                confidence = math.ceil((box.conf[0] * 100)) / 100

                if confidence < 0.5:
                    continue

                # class name
                cls = int(box.cls[0])
                cls_name = classNames[cls]

                if cls_name in objectsFreq:
                    objectsFreq[cls_name].append(confidence)
                else:
                    objectsFreq[cls_name] = [confidence]

                # object details
                org = [x1, y1]
                font = cv2.FONT_HERSHEY_SIMPLEX
                fontScale = 1

                color = (255, 0, 0)

                if cls_name in thread_objects:
                    color = (0, 0, 255)

                if cls_name in thread_objects and not email_sent:
                    with app.app_context():

                        upload_frame_to_firebase(
                            frame, user_id, period.strftime("%Y-%m-%d %H:%M:%S")
                        )

                        send_email_in_thread(
                            f"Security Alert: Unauthorized Object Detected\n\n"
                            f"Object: {cls_name.capitalize()}\n"
                            f"Detection Time: {period.strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"Confidence Level: {confidence * 100:.1f}%\n\n"
                            f"Description:\n"
                            f"The object '{cls_name}' was detected by our security system at the specified time and location. "
                            f"The detection was made with a confidence level of {confidence * 100:.1f}%. "
                            f"Please review the attached image for visual confirmation and take necessary action.\n\n"
                            f"This is an automated alert generated by our security monitoring system. "
                            f"If you have any questions or concerns, please contact the security team.\n\n"
                            f"Thank you for your prompt attention to this matter.\n\n"
                            f"Regards,\n"
                            f"SuRक्षा AI",
                            "Security Alert: Unauthorized Object Detected",
                            user_email,
                            [user_email],
                        )
                        email_sent = True

                thickness = 2

                cv2.putText(
                    frame,
                    f"{classNames[cls]} {confidence * 100:.1f}%",
                    org,
                    font,
                    fontScale,
                    color,
                    thickness,
                )

            for obj in objectsFreq:
                if obj in objectData:
                    objectData[obj]["freq"] = max(
                        objectData[obj]["freq"], len(objectsFreq[obj])
                    )
                    objectData[obj]["maxConfidence"] = max(
                        objectData[obj]["maxConfidence"], max(objectsFreq[obj])
                    )
                    objectData[obj]["minConfidence"] = min(
                        objectData[obj]["minConfidence"], min(objectsFreq[obj])
                    )

                else:
                    objectData[obj] = {
                        "freq": len(objectsFreq[obj]),
                        "maxConfidence": max(objectsFreq[obj]),
                        "minConfidence": min(objectsFreq[obj]),
                        "time": period.strftime("%Y-%m-%d %H:%M:%S"),
                    }

        if period >= next_time:
            next_time += delta
            send_analytics(objectData, user_id)
            objectData = {}
            email_sent = False

        if not success:
            break
        else:
            ret, buffer = cv2.imencode(".jpg", frame)
            frame = buffer.tobytes()
            yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")


@app.route("/")
def index():
    return render_template(
        "index.html",
    )


@app.route("/video")
def video():
    if session.get("user") is None:
        return redirect("/signin")

    camera_feed = request.args.get("camera_feed", False) == "True"

    return render_template("video.html", cameras=get_cameras(), camera_feed=camera_feed)


@app.route("/video_feed")
def video_feed():
    return Response(
        gen_frames(
            session["user"]["localId"] if session.get("user") is not None else None,
            session["user"]["email"] if session.get("user") is not None else None,
        ),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user") is not None:
        return redirect("/profile")

    if request.method == "POST":
        first_name = request.form["first_name"]
        last_name = request.form["last_name"]
        email = request.form["email"]
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if (
            first_name == ""
            or last_name == ""
            or email == ""
            or password == ""
            or confirm_password == ""
        ):
            return render_template("signup.html", error="All fields are required")

        if password != confirm_password:
            return render_template("signup.html", error="Passwords do not match")

        user = auth.create_user_with_email_and_password(email, password)
        user = auth.update_profile(
            id_token=user["idToken"], display_name=first_name + " " + last_name
        )

        session["user"] = user

        return redirect("/profile")

    return render_template("signup.html")


@app.route("/signin", methods=["GET", "POST"])
def signin():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        if email == "" or password == "":
            return render_template("signin.html", error="All fields are required")

        try:
            user = auth.sign_in_with_email_and_password(email, password)
            session["user"] = user
            return redirect("/profile")

        except Exception as e:
            return render_template(
                "signin.html", error="Invalid email or password", error_message=str(e)
            )

    return render_template("signin.html")


@app.route("/signout")
def signout():
    session.pop("user", None)
    return render_template("signin.html")


@app.route("/profile")
def profile():
    if session.get("user") is None:
        return redirect("/signin")

    return render_template("profile.html", user=session.get("user"))


@app.route("/dashboard")
def dashboard():
    if session.get("user") is None:
        return redirect("/signin")

    doc = db.child("analytics").child(session["user"]["localId"]).get()
    data = doc.val()

    if data is None:
        data = {}

    # reduce data to 10 most recent entries
    data = dict(list(data.items())[-15:])

    return render_template(
        "dashboard.html",
        user=session.get("user"),
        data=data,
        images=get_images(session["user"]["localId"]),
    )


@app.route("/clear_logs", methods=["POST"])
def clear_logs():
    if session.get("user") is None:
        return redirect("/signin")

    db.child("analytics").child(session["user"]["localId"]).remove()

    return redirect("dashboard")


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=3000)
