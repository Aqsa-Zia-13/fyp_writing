from django.urls import path, re_path
from .views import task_detail, task_image, submit_response

urlpatterns = [
    path('api/tasks/<int:task_number>/', task_detail),
    path('api/responses/', submit_response),
    re_path(r'^api/task-images/(?P<filename>[^/]+)/$', task_image),
]
