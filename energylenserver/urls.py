from django.conf.urls import patterns, url
from constants import *

urlpatterns = patterns('energylenserver.views',
                       url(r'^data/upload/', 'upload_data'),
                       url(r'^device/register/', 'register_device'),
                       url(r'^' + REAL_TIME_POWER_API, 'real_time_data_access'),
                       url(r'^' + TRAINING_API, 'training_data'),
                       # url(r'^predict', 'predict'),
                       # url(r'^realtime', 'realtime'),
                       # url(r'^final', 'final'),
                       # url(r'^getroomtemperature', 'getroomtemperature'),
                       )
