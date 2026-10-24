from django.urls import path, re_path
from .views import available_tests, combined_score, score_detail, task_detail, task_image, submit_response

urlpatterns = [
    path('api/tests/', available_tests),
    path('api/tasks/<int:task_number>/', task_detail),
    path('api/responses/', submit_response),
    path('api/scores/combined/', combined_score),
    path('api/scores/<int:task_number>/', score_detail),
    re_path(r'^api/task-images/(?P<filename>[^/]+)/$', task_image),
]
