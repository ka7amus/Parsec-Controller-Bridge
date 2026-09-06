import pygame, time

pygame.init()
pygame.joystick.init()
j = pygame.joystick.Joystick(0)
j.init()
print(f"Reading: {j.get_name()}")
print("Press one button at a time. Ctrl+C to stop.")

last = set()
while True:
    pygame.event.pump()
    pressed = {i for i in range(j.get_numbuttons()) if j.get_button(i)}
    if pressed != last:
        print("pressed:", sorted(pressed))
        last = pressed
    time.sleep(0.05)