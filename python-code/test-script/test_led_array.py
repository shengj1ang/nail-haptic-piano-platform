from led_controller import LEDArrayController

with LEDArrayController() as led:
    led.off()
    
    #黑1
    led.set_pixel(1, 0, 0, 255, 0, 80)
    led.set_pixel(1, 2, 0, 255, 0, 80)
    
    #黑2
    led.set_pixel(1, 4, 255, 0, 0, 80)
    led.set_pixel(1, 5, 255, 0, 0, 80)    # strip1, 第5颗, 绿色, 亮度80

    #黑3
    led.set_pixel(1, 9, 0, 0, 255, 80)
    led.set_pixel(1, 10, 0, 0, 255, 80)

    #黑4
    led.set_pixel(1, 12, 255, 0, 0, 80)
    led.set_pixel(1, 13, 255, 0, 0, 80)

    #黑5
    led.set_pixel(1, 15, 0, 255, 0, 80)
    led.set_pixel(1, 16, 0, 255, 0, 80)

    #黑6
    led.set_pixel(1, 20, 0, 0, 255, 80)
    led.set_pixel(1, 21, 0, 0, 255, 80)

    #黑7
    led.set_pixel(1, 23, 255, 0, 0, 80)
    led.set_pixel(1, 25, 255, 0, 0, 80)

    #黑8
    led.set_pixel(1, 28, 0, 255, 0, 80)
    led.set_pixel(1, 30, 0, 255, 0, 80)

    #黑9
    led.set_pixel(1, 32, 0, 0, 255, 80)
    led.set_pixel(1, 33, 0, 0, 255, 80)


    #黑10
    led.set_pixel(1, 35, 255, 0, 0, 80)
    led.set_pixel(1, 36, 255, 0, 0, 80)


    led.set_pixel(0, 0, 255, 0, 0, 128)   # strip0, 第0颗, 红色, 亮度128
    led.set_pixel(0, 1, 0, 255, 0, 128)
    led.set_pixel(0, 2, 0, 0, 255, 128)
    led.set_pixel(0, 3, 255, 0, 0, 128)
    led.set_pixel(0, 4, 0, 255, 0, 128)
    led.set_pixel(0, 5, 0, 0, 255, 128)
    led.set_pixel(0, 6, 255, 0, 0, 128)
    led.set_pixel(0, 7, 0, 255, 0, 128)
    led.set_pixel(0, 8, 0, 0, 255, 128)
    led.set_pixel(0, 9, 255, 0, 0, 128)
    led.set_pixel(0, 10, 0, 255, 0, 128)
    led.set_pixel(0, 11, 0, 0, 255, 128)
    led.set_pixel(0, 12, 255, 0, 0, 128)
    led.set_pixel(0, 13, 0, 255, 0, 128)
    led.set_pixel(0, 14, 0, 0, 255, 128)

    led.set_global_brightness(100)
