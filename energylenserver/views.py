# from django.shortcuts import render
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.conf import settings

from energylenserver.common_imports import *
from energylenserver.models.models import *
from energylenserver.models.DataModels import *
from energylenserver.meter.functions import *
from energylenserver.meter.smap import *
from energylenserver.functions import *
from energylenserver.preprocessing import wifi
from energylenserver.api.reassign import *
from energylenserver.tasks import phoneDataHandler
from energylenserver.models import functions as mod_func
from energylenserver.constants import appliance_dict

import os
import sys
import json
import time
import pandas as pd
import datetime as dt


# Enable Logging
logger = logging.getLogger('energylensplus_django')
upload_logger = logging.getLogger('energylensplus_upload')

"""
Registration API
"""


@csrf_exempt
def register_device(request):
    """
    Receives the registration requests from the mobile devices and
    stores user-device and meter details in the database
    """

    try:
        if request.method == 'GET':
            return HttpResponse(json.dumps(ERROR_INVALID_REQUEST), content_type="application/json")

        if request.method == 'POST':
            logger.info("[POST Request Received] - %s", sys._getframe().f_code.co_name)

            # Store the Unknown user if it does not exist
            ukwn_count = RegisteredUsers.objects.filter(apt_no__exact=0000).count()
            if ukwn_count == 0:

                # Store user
                ukwn_user = RegisteredUsers(dev_id=unknown_id, reg_id='ABCDEF', apt_no=0000,
                                            name='Unknown', is_active=False,
                                            email_id='', phone_model='Unknown')
                all_user = RegisteredUsers(dev_id=all_id, reg_id='ABCDEFGHRITIGHKD', apt_no=9999,
                                           name='All', is_active=False,
                                           email_id='', phone_model='All')
                ukwn_user.save()
                all_user.save()
                logger.debug("Unknown and All users created!")

            # print request.body
            payload = json.loads(request.body)
            # logger.debug("POST Payload:\n%s", payload)

            reg_id = payload['registration_id']
            user_name = payload['user_name']
            email_id = payload['email_id']
            dev_id = payload['dev_id']
            phone_model = payload['phone_model']
            apt_no = payload['apt_no']
            home_ap = payload['home_ap']
            other_ap = payload['other_ap']

            logger.debug("--User Registration Details--")
            logger.debug("RegID: %s", reg_id)
            logger.debug("Username: %s", user_name)
            logger.debug("Email ID: %s", email_id)
            logger.debug("Device ID: %s", dev_id)
            logger.debug("Model: %s", phone_model)
            logger.debug("Apartment Number: %s", apt_no)
            logger.debug("Home AP: %s", home_ap)
            logger.debug("Other APs: %s", other_ap)

            if apt_no.isdigit():

                # Store the meter details of the apt_no if the current user is the first
                # member of the house that registers

                apt_no = int(apt_no)
                user_count = RegisteredUsers.objects.filter(apt_no__exact=apt_no).count()
                logger.debug("Number of users registered for %d:%s", apt_no, user_count)
                if user_count == 0:
                    # Get meter information for the apt_no for the apartment

                    # TEST CODE: for test apartment 102A
                    if apt_no in [102, '102A']:
                        if isinstance(apt_no, int):
                            apt_no = '102A'
                        else:
                            apt_no = 102
                    meters = get_meter_info(apt_no)

                    # Store meter information in the DB
                    for meter in meters:
                        meter_uuid = meter['uuid']
                        meter_type = meter['type']
                        if meter_type == "Light Backup":
                            meter_type = "Light"

                        # TEST CODE: for test apartment 102A
                        if apt_no in [102, '102A']:
                            apt_no = 102
                        minfo_record = MeterInfo(
                            meter_uuid=meter_uuid, meter_type=meter_type, apt_no=apt_no)
                        minfo_record.save()

                logger.debug("Home AP:%s", home_ap)

                try:
                    # TEST CODE: for test apartment 102A
                    if apt_no in [102, '102A']:
                        apt_no = 102
                    # Delete existing records
                    AccessPoints.objects.filter(apt_no=apt_no).delete()

                    # Store the access point details
                    ap_record = AccessPoints(
                        apt_no=apt_no, macid=home_ap['macid'], ssid=home_ap['ssid'], home_ap=True)
                    ap_record.save()

                    for ap in other_ap:
                        ap_record = AccessPoints(
                            apt_no=apt_no, macid=ap['macid'], ssid=ap['ssid'], home_ap=False)
                        ap_record.save()
                except Exception, e:
                    logger.exception("[APSaveException]::%s" % (e))

            try:
                r = RegisteredUsers.objects.get(dev_id=dev_id)
                logger.debug("Registration with device ID %s exists", r.dev_id)
                # Store user
                r.reg_id = reg_id
                r.name = user_name
                r.is_active = True
                # TEST CODE: for test apartment 102A
                if apt_no in [102, '102A']:
                    apt_no = 102
                if apt_no != 0:
                    r.apt_no = apt_no
                r.modified_date = dt.datetime.fromtimestamp(time.time())
                r.save()
                logger.debug("Registration updated")
            except RegisteredUsers.DoesNotExist, e:

                # TEST CODE: for test apartment 102A
                if apt_no in [102, '102A']:
                    apt_no = 102
                # Store user
                user = RegisteredUsers(dev_id=dev_id, reg_id=reg_id, apt_no=apt_no, name=user_name,
                                       email_id=email_id, phone_model=phone_model)
                user.save()
                logger.debug("Registration successful")
            return HttpResponse(json.dumps(REGISTRATION_SUCCESS),
                                content_type="application/json")

    except Exception, e:

        if str(e) == "request data read error":
            logger.error("[DeviceRegistrationException Occurred]::%s", e)
        else:
            logger.exception("[DeviceRegistrationException Occurred]::%s", e)
        return HttpResponse(json.dumps(REGISTRATION_UNSUCCESSFUL), content_type="application/json")

"""
Training API
"""


@csrf_exempt
def training_data(request):
    """
    Receives the training data labels, computes power consumption,
    and stores them as Metadata
    """

    try:
        if request.method == 'GET':
            return HttpResponse(json.dumps(ERROR_INVALID_REQUEST), content_type="application/json")

        if request.method == 'POST':
            logger.info("[POST Request Received] - %s", sys._getframe().f_code.co_name)
            payload = json.loads(request.body)

            dev_id = payload['dev_id']
            start_time = payload['start_time']
            end_time = payload['end_time']
            location = payload['location']
            appliance = payload['appliance']
            audio_based = payload['audio_based']
            presence_based = payload['presence_based']

            logger.debug("payload: %s", payload)

            # Check if it is a registered user
            user = mod_func.get_user(dev_id)
            if isinstance(user, bool):
                return HttpResponse(json.dumps(TRAINING_UNSUCCESSFUL),
                                    content_type="application/json")
            else:
                apt_no = user.apt_no
                logger.debug("Apartment Number: %d", apt_no)

            # Compute Power
            power = training_compute_power(apt_no, start_time, end_time)
            logger.debug("Computed Power:: %f", power)

            # See if entry exists for appliance-location combination
            # Update power value if it exists
            if power >= thresmin:
                app_arr = appliance.split('-')
                if len(app_arr) > 1:
                    appliance = app_arr[0]
                    how_many = int(app_arr[1])
                else:
                    how_many = 1
                try:
                    # Update power
                    records = Metadata.objects.filter(apt_no__exact=apt_no,
                                                      location__exact=location,
                                                      appliance__exact=appliance,
                                                      presence_based=presence_based,
                                                      audio_based=audio_based)
                    if records.count() == 1:
                        if how_many > records[0].how_many:
                            records.update(how_many=how_many)
                        else:
                            records.update(power=power)
                        logger.debug(
                            "Metadata with entry:%d %s %s exists", apt_no, appliance, location)
                        logger.debug("Metadata record updated")
                    else:
                        # Store metadata
                        metadata = Metadata(apt_no=apt_no,
                                            presence_based=presence_based, audio_based=audio_based,
                                            appliance=appliance, location=location, power=power,
                                            how_many=how_many)
                        metadata.save()
                        logger.debug("Metadata creation successful!")
                except Metadata.DoesNotExist, e:

                    # Store metadata
                    metadata = Metadata(apt_no=apt_no,
                                        presence_based=presence_based, audio_based=audio_based,
                                        appliance=appliance, location=location, power=power,
                                        how_many=how_many)
                    metadata.save()
                    logger.debug("Metadata creation successful")

            payload = {}
            payload['power'] = power
            return HttpResponse(json.dumps(payload),
                                content_type="application/json")

    except Exception, e:

        if str(e) == "request data read error":
            logger.error("[TrainingDataException Occurred]::%s", e)
        else:
            logger.exception("[TrainingDataException Occurred]::%s", e)
        return HttpResponse(json.dumps(TRAINING_UNSUCCESSFUL),
                            content_type="application/json")


"""
Upload API
"""


def determine_user(filename):
    """
    Determines existence of user based on dev_id
    """
    # Find the sensor from the filename and choose appropriate table
    filename_l = filename.split('_')
    device_id = int(filename_l[0])

    # Check if it is a registered user
    is_user = mod_func.get_user(device_id)
    return is_user


def import_from_file(filename, csvfile):
    """
    Imports the CSV file into appropriate db model
    """

    filename_l = filename.split('_')

    user = determine_user(filename)
    if isinstance(user, bool):
        return False

    upload_logger.debug("User: %s", user.name)
    # upload_logger.debug("File size: %s", csvfile.size)

    training_status = False

    sensor_name = filename_l[2]
    if sensor_name == "Training":
        sensor_name = filename_l[3]
        training_status = True
    upload_logger.debug("Sensor: %s", sensor_name)

    # Save file in a temporary location
    new_filename = ('data_file_' + sensor_name + '_' + str(
        user.dev_id) + '_' + filename_l[-2] + "_" + filename_l[-1] +
        "_" + str(int(time.time())) + '.csv')
    path = default_storage.save(new_filename, ContentFile(csvfile.read()))
    filepath = os.path.join(settings.MEDIA_ROOT, path)

    # Create a dataframe for preprocessing
    if sensor_name != 'rawaudio':
        try:
            df_csv = pd.read_csv(filepath, error_bad_lines=False)

            if "label" not in df_csv.columns:
                upload_logger.exception("[DataFileFormatIncorrect] Header missing!")
                os.remove(filepath)
                return False

            # --Preprocess records before storing--
            if sensor_name == 'wifi':
                if len(df_csv) == 0:
                    os.remove(filepath)
                    upload_logger.exception("[DataFileFormatIncorrect] "
                                            "Empty wifi file sent. Upload not successful!")
                    return True

                df_csv = wifi.format_data(df_csv)

                if df_csv is False:
                    os.remove(filepath)
                    upload_logger.exception("[DataFileFormatIncorrect] "
                                            "Incorrect wifi file sent. Upload not successful!")
                    return False

                # Create temp wifi csv file
                os.remove(filepath)
                df_csv.to_csv(filepath, index=False)
        except pd.parser.CParserError, e:
            upload_logger.exception("[DataFileFormatIncorrect] Upload unsuccessful! :: %s", e)
            os.remove(filepath)  # Commented for debugging
            return True

        except Exception, e:
            if str(e) == "Passed header=0 but only 0 lines in file":
                upload_logger.exception(
                    "[Exception]:: Creation of dataframe failed! No lines found in the file!")
                os.remove(filepath)
                return False
            else:
                upload_logger.exception("[DataFileFormatIncorrect]:: %s", e)
                os.remove(filepath)
                return False

    # Call new celery task for importing records
    phoneDataHandler.delay(filename, sensor_name, filepath, training_status, user)

    return True


@csrf_exempt
def upload_data(request):
    """
    Receives the uploaded CSV files and stores them in the database
    """

    try:
        if request.method == 'GET':
            return HttpResponse(json.dumps(ERROR_INVALID_REQUEST), content_type="application/json")

        if request.method == 'POST':
            upload_logger.info("[POST Request Received] - %s" % sys._getframe().f_code.co_name)

            payload = request.FILES
            file_container = payload['uploadedfile']
            filename = str(file_container.name)
            csvfile = file_container
            upload_logger.debug("File received:%s", filename)

            # Store in the database
            if(import_from_file(filename, csvfile)):
                return HttpResponse(json.dumps(UPLOAD_SUCCESS), content_type="application/json")

            else:
                return HttpResponse(json.dumps(UPLOAD_UNSUCCESSFUL),
                                    content_type="application/json")

    except Exception, e:

        if str(e) == "request data read error":
            upload_logger.error("[UploadDataException Occurred] Request Data Error::%s", e)
            # upload_logger.debug("Request body:: %s", request)
        else:
            upload_logger.exception("[UploadDataException Occurred]::%s", e)
        return HttpResponse(json.dumps(UPLOAD_UNSUCCESSFUL), content_type="application/json")


@csrf_exempt
def upload_stats(request):
    """
    Receives the uploaded notification CSV files and stores them in the database
    """

    try:
        if request.method == 'GET':
            return HttpResponse(json.dumps(ERROR_INVALID_REQUEST), content_type="application/json")

        if request.method == 'POST':
            logger.info("[POST Request Received] - %s", sys._getframe().f_code.co_name)

            payload = request.FILES
            file_container = payload['uploadedfile']
            filename = str(file_container.name)
            csvfile = file_container
            logger.debug("File received:%s", filename)

            # --- Saving file in the database ---
            # Check if it is a registered user
            user = determine_user(filename)
            if isinstance(user, bool):
                return False

            # Find the file type from the filename and choose appropriate table
            filename_l = filename.split('_')
            file_type = filename_l[1]

            if file_type == "screen":
                file_tag = "screenlog"
                model = UsageLogScreens
            else:
                file_tag = "battery"
                model = BatteryUsage

            # Save file in a temporary location
            new_filename = ('stats_' + file_tag + '_' + str(
                user.dev_id) + '_' + time.ctime(time.time()) + '.csv')
            path = default_storage.save(new_filename, ContentFile(csvfile.read()))
            filepath = os.path.join(settings.MEDIA_ROOT, path)

            # Store in the database
            model().save_stats(user, filepath)
            return HttpResponse(json.dumps(UPLOAD_SUCCESS), content_type="application/json")

    except Exception, e:

        if str(e) == "request data read error":
            logger.error("[UploadStatsException Occurred] Request Data Error::%s", e)
        else:
            logger.exception("[UploadStatsException Occurred]::%s", e)
        return HttpResponse(json.dumps(UPLOAD_UNSUCCESSFUL), content_type="application/json")

"""
Real Time Power Plots API
"""


@csrf_exempt
def real_time_data_access(request):
    """
    Receives real-time data access request for plots
    """

    try:
        if request.method == 'GET':
            return HttpResponse(json.dumps(ERROR_INVALID_REQUEST), content_type="application/json")

        if request.method == 'POST':

            # TODO: Get apt_no from the db based on the IMEI number
            payload = json.loads(request.body)
            logger.info("[POST Request Received] - %s", sys._getframe().f_code.co_name)

            dev_id = payload['dev_id']
            upload_logger.debug("Requested by:%s", dev_id)

            # Check if it is a registered user
            is_user = mod_func.get_user(dev_id)
            if isinstance(is_user, bool):
                return HttpResponse(json.dumps(REALTIMEDATA_UNSUCCESSFUL),
                                    content_type="application/json")
            else:
                apt_no = is_user.apt_no
                upload_logger.debug("Apartment Number:%d", apt_no)

            # Get power data
            timestamp, total_power = get_latest_power_data(apt_no)

            payload = {}
            payload[timestamp] = total_power

            upload_logger.debug("Payload: %s", payload)

            return HttpResponse(json.dumps(payload), content_type="application/json")

    except Exception, e:

        logger.exception("[RealTimeDataException Occurred]::%s", e)
        return HttpResponse(json.dumps(REALTIMEDATA_UNSUCCESSFUL),
                            content_type="application/json")


@csrf_exempt
def real_time_past_data(request):
    """
    Receives first real-time data access request for plots
    """

    try:
        if request.method == 'GET':
            return HttpResponse(json.dumps(ERROR_INVALID_REQUEST), content_type="application/json")

        if request.method == 'POST':

            payload = json.loads(request.body)
            logger.info("[POST Request Received] - %s", sys._getframe().f_code.co_name)

            dev_id = payload['dev_id']
            minutes = int(payload['minutes'])
            upload_logger.debug("Requested by:%d", dev_id)
            upload_logger.debug("For %d minute(s)", minutes)

            # Check if it is a registered user
            is_user = mod_func.get_user(dev_id)
            if isinstance(is_user, bool):
                return HttpResponse(json.dumps(REALTIMEDATA_UNSUCCESSFUL),
                                    content_type="application/json")
            else:
                apt_no = is_user.apt_no
                upload_logger.debug("Apartment Number:%s", apt_no)

            # Get power data
            end_time = time.time()
            start_time = end_time - 60 * minutes

            s_time = timestamp_to_str(start_time, date_format)
            e_time = timestamp_to_str(end_time, date_format)
            data_df_list = get_meter_data_for_time_slice(apt_no, s_time, e_time)

            if len(data_df_list) == 0:
                upload_logger.debug("No data to send")
                return HttpResponse(json.dumps({}), content_type="application/json")

            # Creation of the payload
            payload = {}
            if len(data_df_list) > 1:
                # Combine the power and light streams
                df = combine_streams(data_df_list)
            else:
                df = data_df_list[0].copy()

            for idx in df.index:
                payload[df.ix[idx]['time']] = df.ix[idx]['power']

            payload_body = {}
            # Sorting payload
            for key in sorted(payload.iterkeys()):
                payload_body[key] = payload[key]

            upload_logger.debug("Payload Size:%s", len(payload_body))
            # logger.debug("Payload", json.dumps(payload_body, indent=4)

            return HttpResponse(json.dumps(payload_body), content_type="application/json")

    except Exception, e:
        logger.exception("[RealTimePastDataException Occurred]::%s", e)
        return HttpResponse(json.dumps(REALTIMEDATA_UNSUCCESSFUL),
                            content_type="application/json")


"""
Reassigning Inferences API
"""


@csrf_exempt
def reassign_inference(request):
    """
    Receives the ground truth validation report with corrected labels
    """
    try:
        if request.method == 'GET':
            return HttpResponse(json.dumps(ERROR_INVALID_REQUEST), content_type="application/json")

        if request.method == 'POST':
            payload = json.loads(request.body)
            logger.info("[POST Request Received] - %s", sys._getframe().f_code.co_name)

            dev_id = payload['dev_id']

            # Check if it is a registered user
            user = mod_func.get_user(dev_id)
            if isinstance(user, bool):
                return HttpResponse(json.dumps(REASSIGN_UNSUCCESSFUL),
                                    content_type="application/json")
            else:
                apt_no = user.apt_no
                logger.debug("Apartment Number:%s", apt_no)

            logger.debug("Correcting inferences..")
            options = payload['options']

            # Reassign the specified activity and update the db
            status = correct_inference(user, options)

            payload = {}
            payload['status'] = status

            return HttpResponse(json.dumps(payload),
                                content_type="application/json")

    except Exception, e:
        logger.exception("[ReassignInferenceException Occurred]::", e)
        return HttpResponse(json.dumps(REASSIGN_UNSUCCESSFUL),
                            content_type="application/json")


@csrf_exempt
def test_function_structure(request):
    """
    Receives the uploaded CSV files and stores them in the database
    """

    try:
        if request.method == 'GET':
            return HttpResponse(json.dumps(ERROR_INVALID_REQUEST), content_type="application/json")

        if request.method == 'POST':
            pass
    except Exception, e:

        logger.exception("[TrainingDataException Occurred]::", e)
        return HttpResponse(json.dumps(TRAINING_UNSUCCESSFUL),
                            content_type="application/json")


@csrf_exempt
def test_xmppclient(request):
    pass
