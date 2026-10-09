from django.urls import path
from .views import task_detail, submit_response

urlpatterns = [
    path('api/tasks/<int:task_number>/', task_detail),
    path('api/responses/', submit_response),
]
